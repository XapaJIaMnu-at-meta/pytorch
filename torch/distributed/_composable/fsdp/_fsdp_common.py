import traceback

from dataclasses import dataclass
from enum import auto, Enum
from typing import Any, cast, List, Optional, Tuple

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.distributed._composable.contract import _get_registry
from torch.distributed._tensor import DeviceMesh, DTensor, Placement


@dataclass
class DataParallelMeshInfo:
    mesh: DeviceMesh
    shard_mesh_dim: Optional[int] = None
    replicate_mesh_dim: Optional[int] = None

    def __post_init__(self):
        if self.shard_mesh_dim is None and self.replicate_mesh_dim is None:
            raise AssertionError(
                "At least one of shard_mesh_dim and replicate_mesh_dim must not be None"
            )


@dataclass
class FSDPMeshInfo(DataParallelMeshInfo):
    def __post_init__(self):
        super().__post_init__()
        if self.shard_mesh_dim is None:
            raise AssertionError("Expects non-None shard_mesh_dim")
        self.shard_mesh_size: int = self.mesh.size(self.shard_mesh_dim)
        self.shard_process_group = cast(
            dist.ProcessGroup, self.mesh.get_group(self.shard_mesh_dim)
        )
        self.shard_mesh_rank: int = self.shard_process_group.rank()


@dataclass
class DDPMeshInfo(DataParallelMeshInfo):
    def __post_init__(self):
        super().__post_init__()
        if self.replicate_mesh_dim is None:
            raise AssertionError("Expects non-None replicate_mesh_dim")
        self.replicate_mesh_size: int = self.mesh.size(self.replicate_mesh_dim)
        self.replicate_process_group = cast(
            dist.ProcessGroup, self.mesh.get_group(self.replicate_mesh_dim)
        )
        self.replicate_mesh_rank: int = self.replicate_process_group.rank()


@dataclass
class HSDPMeshInfo(FSDPMeshInfo, DDPMeshInfo):
    def __post_init__(self):
        super(FSDPMeshInfo, self).__post_init__()
        super(DDPMeshInfo, self).__post_init__()


class TrainingState(Enum):
    """Describes the training state of one FSDP state / parameter group."""

    # Transition to forward starting pre-forward until post-forward
    FORWARD = auto()
    # Transition to pre-backward when unsharding in backward
    PRE_BACKWARD = auto()
    # Transition to post-backward when resharding and reducing gradients
    POST_BACKWARD = auto()
    # Idle before/after forward or before pre-backward/after post-backward
    IDLE = auto()


def _raise_assert_with_print(*args: Any, **kwargs: Any):
    print(f"[Rank {dist.get_rank()}] ", end="")
    print(*args, **kwargs)
    traceback.print_stack()
    raise AssertionError(*args, **kwargs)


def _is_composable_with_fsdp(module: nn.Module) -> bool:
    registry = _get_registry(module)
    if registry is None:
        return True
    # Registry keys by function name
    return "replicate" not in registry


def _chunk_with_empty(
    tensor: torch.Tensor, num_chunks: int, dim: int
) -> List[torch.Tensor]:
    chunks = list(torch.chunk(tensor, num_chunks, dim=dim))
    while len(chunks) < num_chunks:
        chunks.append(chunks[0].new_empty(0))
    return chunks


def _from_local_no_grad(
    local_tensor: torch.Tensor,
    device_mesh: DeviceMesh,
    placements: Tuple[Placement, ...],
    global_size: torch.Size,
    global_stride: Tuple[int, ...],
) -> DTensor:
    """
    This method is similar to ``DTensor.from_local()`` except it avoids some
    CPU overhead by avoiding default args and not being differentiable.
    """
    return DTensor(
        # Use the local tensor directly instead of constructing a new tensor
        # variable, e.g. with `view_as()`, since this is not differentiable
        local_tensor,
        device_mesh,
        placements,
        shape=global_size,
        dtype=local_tensor.dtype,
        requires_grad=local_tensor.requires_grad,
        stride=global_stride,
    )
