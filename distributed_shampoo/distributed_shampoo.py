"""
Copyright (c) Meta Platforms, Inc. and affiliates.
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.

"""

import logging
import os
from collections import abc as container_abcs, defaultdict
from copy import deepcopy
from itertools import chain
from typing import Any, Dict, List, Tuple

import torch
import torch.distributed as dist

from .shampoo_dist_utils import (
    distribute_buffer_sizes,
    split_local_dist_buffers,
)

from .shampoo_utils import (
    AdagradPreconditioner,
    BlockShampooPreconditioner,
    DistributedPreconditioner,
    GraftingType,
    LargeDimMethod,
    Preconditioner,
    ShampooPreconditioner,
)

logger = logging.getLogger(__name__)

BETAS = "betas"
EXP_AVG = "exp_avg"
EPSILON = "epsilon"
GRAFTING_BETA2 = "grafting_beta2"
GRAFTING_EPSILON = "grafting_epsilon"
GRAFTING_MOMENTUM = "grafting_momentum"
LR = "lr"
MOMENTUM = "momentum"
PARAMS = "params"
PRECONDITIONERS = "preconditioners"
STEP = "step"
WEIGHT_DECAY = "weight_decay"
DIST_BUFFER = "dist_buffer"
MY_DIST_BUFFER = "my_dist_buffer"


class DistributedShampoo(torch.optim.Optimizer):
    """Implements distributed Shampoo algorithm.

    Implemented by:
        Hao-Jun Michael Shi (Meta Platforms, Inc.)
        Tsung-Hsien Lee
        Shintaro Iwasaki (Meta Platforms, Inc.)
        Jose Gallego-Posada (MILA / Meta Platforms, Inc.)

    with support from:
        Rohan Anil (Google), Yizi Gu (Meta), Vineet Gupta (Google), Minhui Huang (Meta), Zhijing Li (Meta), Wanchao Liang (Meta),
        Dheevatsa Mudigere (Nvidia), Mike Rabbat (Meta), Kaushik Rangadurai (Meta), and Xunnan (Shawn) Xu (Meta).

    Partly based on the work in:
    - https://arxiv.org/pdf/1802.09568.pdf
    - https://arxiv.org/pdf/2002.09018.pdf

    Uses infinity norm to evaluate residuals and errors. By default, grafts from Adagrad.

    ------------
    Requirements
    ------------

    1. PyTorch >= 1.13
    2. Python >= 3.8
    3. CUDA 11.3, 11.4, 12

    If one wants to use DTensor which leads to memory savings, please set use_dtensor = True. Requires PyTorch 2 nightly build.

    Note: We have observed known instabilities with the torch.linalg.eigh operator on CUDA 11.6-11.8, specifically for low-rank
    matrices, which may appear with using a small start_preconditioning_step. Please avoid these versions of CUDA if possible.

    --------
    Features
    --------

    1. Layerwise Grafting: In order to tune Shampoo, we can "graft" a layer-wise learning rate schedule from a previous method
        and apply it to Shampoo. This is performed by taking the norm of the layer-wise step of the grafted method, normalizing
        the Shampoo step, and re-scaling the normalized Shampoo step by the product of the norm of the grafted step + learning rate.

        This may be interpreted as an additional block re-scaling of the entire Shampoo preconditioner.
        This is the key ingredient to making Shampoo work in practice.

        We support the following methods:
            - GraftingType.NONE: Performs no grafting.
            - GraftingType.SGD: Grafts the stochastic gradient method.
            - GraftingType.ADAGRAD: Grafts the Adagrad method.
            - GraftingType.RMSPROP: Grafts the RMSProp method.
            - GraftingType.ADAM: Grafts the Adam method.
            - GraftingType.ADAGRAD_NORMALIZED: Grafts the Adagrad method with normalized gradients.
            - GraftingType.RMSPROP_NORMALIZED: Grafts the RMSProp method with normalized gradients.
            - GraftingType.ADAM_NORMALIZED: Grafts the Adam method with normalized gradients.

        NOTE: These methods do not graft the first-moment component - it is entirely based upon grafting using the
        diagonal preconditioner. If using an exponential moving average of the gradient (or gradient filtering), we
        can set beta1 as the same value from before, and both Shampoo and the grafted method will use the filtered
        gradient.

    2. Large-Dimensional Tensors: Supports multiple approaches for scaling Shampoo to tensors with large dimensions.
        For simplicity, we explain using a linear layer/matrix parameter, although this is generalizable to higher-order
        tensors.

        Suppose that W is a m x n matrix, i.e.,

            [[w_11 w_12 ... w_1n]
             [w_21 w_22 ... w_2n]
        W =           :
             [w_m1 w_m2 ... w_mn]]

        - LargeDimMethod.BLOCKING (Default): Given a max_preconditioner_dim tau > 0, blocks W and applies Shampoo to
            each block, i.e., if tau divides both m, n, then:

                [[W_11 W_12 ... W_1k]
                 [W_21 W_22 ... W_2k]
            W =           :
                 [W_l1 W_l2 ... W_lk]]

            and apply Shampoo to W_ij which is a tau x tau matrix. This can be viewed as further blocking each block of the
            block-diagonal preconditioner.

            Computational cost = O(tau^3)
            Memory cost = 4mn (including root inverse preconditioners)

        - LargeDimMethod.ADAGRAD: Given a max_preconditioner_dim tau > 0, checks if any dimensions of the tensor is greater
            than tau. If so, uses Adagrad preconditioner in place of Shampoo. Corresponds to a diagonal preconditioner.

            Computational cost = O(mn)
            Memory cost = mn

        - LargeDimMethod.DIAGONAL: Given a max_preconditioner_dim tau > 0, uses a diagonal Shampoo preconditioner in place of
            the full Shampoo preconditioner. Corresponds to a (crude) diagonal preconditioner.

            Computational cost = O(mn)
            Memory cost = m + n

    3. Distributed Memory and Computation: Supports multi-GPU data-parallel training via torch.distributed by setting
        num_gpus_per_group > 1. Distributes the computation required for Shampoo (updating of the preconditioners, computation
        of the root inverse, preconditioning of the gradients, etc.) across multiple GPUs. The memory is similarly distributed
        using DTensor. num_gpus_per_group specifies the number of GPUs used per distributed group. The computation is
        replicated across different groups.

        Requirements:
        - torch.distributed must be initialized in advance.
        - Only supports homogeneous hardware architectures.

    Args:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float): learning rate (Default: 1e-2)
        betas (Tuple[float, float]): coefficients used for computing running averages
            of gradient and its square (Default: (0.9, 1.0))
        epsilon (float): term added to the denominator to improve numerical stability (Default: 1e-12)
        momentum (float): momentum parameter (default: 0.)
        weight_decay (float): weight decay (L2 penalty) (Default: 0)
        max_preconditioner_dim (int): maximum preconditioner dimension (Default: 1024)
        precondition_frequency (int): frequency for computing root inverse preconditioner (Default: 1)
        start_preconditioning_step (int): iteration to start computing inverse preconditioner. If -1, uses
            the same value as precondition_frequency. (Default: -1)
        exponent_override (int): exponent to use in Shampoo. (Default: 0)
        exponent_multiplier (float): number to be multiplied to the numerator of the inverse root. (Default: 1.0)
        use_nesterov (bool): uses Nesterov momentum (default: False)
        use_bias_correction (bool): flag for using bias correction (Default: True)
        use_decoupled_weight_decay (bool): Flag for using AdamW-style decoupled weight decay (Default: True)
        preconditioner_dtype (torch.dtype): data type for preconditioner (Default: torch.float)
        large_dim_method (LargeDimMethod): method for handling large scale tensors. (Default: LargeDimMethod.BLOCKING)
        num_gpus_per_group (int): number of GPUs per distributed process group for distributed computation/memory.
            If num_gpus_per_group = -1 is used, then defaults to using the LOCAL_WORLD_SIZE. (Default: -1)
        use_merge_dims (bool): merge dimensions if possible while respecting max_preconditioner_dim. (Default: True)
        grafting_type (GraftingType): selects grafting method. (Default: GraftingType.ADAGRAD)
        grafting_epsilon (float): epsilon for grafting method. (Default: 1e-3)
        grafting_beta2 (float): exponential moving average factor for grafting method. (Default: 1.0)
        use_protected_eigh (bool): Flag for using two guards to prevent failures of torch.linalg.eigh. (Default: True)
            1. Attempts to compute root inverse in preconditioner_dtype precision.
            2. Attempts to recompute the eigendecomposition if using lower-precision fails.
            3. Otherwise, re-uses previous inverse factor matrix when both root inverse computations fail.
        use_dtensor (bool): use DTensor. Requires PyTorch 2 nightly. Otherwise, uses Tensor. (Default: True)
        debug_mode (bool): debugging mode. Uses more memory to compute error to fp64 case. Must enable logging level to
            DEBUG. (Default: False)

    """

    def __init__(
        self,
        params,
        lr: float = 1e-2,
        betas: Tuple[float, float] = (0.9, 1.0),
        epsilon: float = 1e-12,
        momentum: float = 0.0,
        weight_decay: float = 0.0,
        max_preconditioner_dim: int = 1024,
        precondition_frequency: int = 1,
        start_preconditioning_step: int = -1,
        exponent_override: int = 0,
        exponent_multiplier: float = 1.0,
        use_nesterov: bool = False,
        use_bias_correction: bool = True,
        use_decoupled_weight_decay: bool = True,
        preconditioner_dtype: torch.dtype = torch.float,
        large_dim_method: LargeDimMethod = LargeDimMethod.BLOCKING,
        num_gpus_per_group: int = -1,
        use_merge_dims: bool = True,
        grafting_type: GraftingType = GraftingType.ADAGRAD,
        grafting_epsilon: float = 1e-3,
        grafting_beta2: float = 1.0,
        use_protected_eigh: bool = True,
        use_dtensor: bool = True,
        debug_mode: bool = False,
    ):
        # Hyperparameter checks.
        if not lr >= 0.0:
            raise ValueError(f"Invalid learning rate: {lr}. Must be >= 0.0.")
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError(
                f"Invalid beta parameter at index 0: {betas[0]}. Must be in [0.0, 1.0)."
            )
        if not 0.0 < betas[1] <= 1.0:
            raise ValueError(
                f"Invalid beta parameter at index 1: {betas[1]}. Must be in (0.0, 1.0]."
            )
        if not epsilon > 0.0:
            raise ValueError(f"Invalid epsilon value: {epsilon}. Must be > 0.0.")
        if not 0.0 <= momentum < 1.0:
            raise ValueError(
                f"Invalid momentum parameter: {momentum}. Must be [0.0, 1.0)."
            )
        if not weight_decay >= 0.0:
            raise ValueError(
                f"Invalid weight_decay value: {weight_decay}. Must be > 0.0."
            )
        if not max_preconditioner_dim >= 1:
            raise ValueError(
                f"Invalid max preconditioner dimension: {max_preconditioner_dim}. Must be >= 1."
            )
        if not precondition_frequency >= 1:
            raise ValueError(
                f"Invalid precondition frequency: {precondition_frequency}. Must be >= 1."
            )
        if not start_preconditioning_step >= -1:
            raise ValueError(
                f"Invalid start preconditioning step: {start_preconditioning_step}"
            )
        if not num_gpus_per_group >= -1:
            raise ValueError(
                f"Invalid number of GPUs per group: {num_gpus_per_group}. Must be >= -1."
            )
        if not exponent_override >= 0:
            raise ValueError(
                f"Invalid exponent override: {exponent_override}. Must be >= 0."
            )
        if not 0.0 < grafting_beta2 <= 1.0:
            raise ValueError(
                f"Invalid grafting beta parameter: {grafting_beta2}. Must be in (0.0, 1.0]."
            )
        if not grafting_epsilon > 0.0:
            raise ValueError(
                f"Invalid epsilon value: {grafting_epsilon}. Must be > 0.0."
            )

        # Distributed checks.
        if num_gpus_per_group > 1 or num_gpus_per_group == -1:
            if not torch.cuda.is_available():
                raise ValueError("Using distributed version of Shampoo without GPUs!")
            if not dist.is_initialized():
                raise ValueError(
                    "Using distributed version of Shampoo without initializing distributed process group!"
                )

            # Defaults to number of GPUs per node if using -1.
            if num_gpus_per_group == -1:
                num_gpus_per_group = int(
                    os.environ.get("LOCAL_WORLD_SIZE", dist.get_world_size())
                )

            if not dist.get_world_size() >= num_gpus_per_group:
                num_gpus_per_group = dist.get_world_size()
                logger.warning(
                    f"Number of GPUs per group {num_gpus_per_group} is specified larger than global world size {dist.get_world_size()}. Setting to default world size."
                )
            if not dist.get_world_size() % num_gpus_per_group == 0:
                raise ValueError(
                    f"Invalid number of GPUs per group: {num_gpus_per_group}. Must divide global world size {dist.get_world_size()}."
                )
        else:
            num_gpus_per_group = 1

        super(DistributedShampoo, self).__init__(
            params,
            {
                LR: lr,
                BETAS: betas,
                MOMENTUM: momentum,
                WEIGHT_DECAY: weight_decay,
                EPSILON: epsilon,
                GRAFTING_EPSILON: grafting_epsilon,
                GRAFTING_BETA2: grafting_beta2,
            },
        )

        # Initialize algorithm-related fields.
        self._max_preconditioner_dim = max_preconditioner_dim
        self._precondition_frequency = precondition_frequency
        self._exponent_override = exponent_override
        self._exponent_multiplier = exponent_multiplier
        self._num_gpus_per_group = num_gpus_per_group
        self._use_merge_dims = use_merge_dims
        self._large_dim_method = large_dim_method
        self._use_decoupled_weight_decay = use_decoupled_weight_decay
        self._preconditioner_dtype = preconditioner_dtype
        self._use_bias_correction = use_bias_correction
        self._grafting_type = grafting_type
        self._grafting_epsilon = grafting_epsilon
        self._grafting_beta2 = grafting_beta2
        self._parameter_count = 0
        self._use_nesterov = use_nesterov
        self._use_protected_eigh = use_protected_eigh
        self._use_dtensor = use_dtensor
        self._debug_mode = debug_mode
        if self._use_nesterov and momentum == 0.0:
            logger.warning(
                "Nesterov flag is enabled but momentum parameter is zero! Continuing without using momentum or Nesterov acceleration..."
            )

        if start_preconditioning_step == -1:
            self._start_preconditioning_step = precondition_frequency
            logger.warning(
                f"start_preconditioning_step set to -1. Setting start_preconditioning_step equal to precondition frequency {precondition_frequency} by default."
            )
        else:
            self._start_preconditioning_step = start_preconditioning_step

        # Initialize comms-related fields.
        self._world_size = dist.get_world_size() if self._use_distributed() else 1

        # Initialize Shampoo preconditioners and distributed buffers.
        self._dist_group = None
        buffer_ranks_list = self._assign_preconditioners_to_ranks()
        self._initialize_preconditioners_and_steps(buffer_ranks_list)

    @torch.no_grad()
    def _use_distributed(self) -> bool:
        return self._num_gpus_per_group > 1

    @torch.no_grad()
    def _initialize_preconditioners_and_steps(
        self, buffer_ranks_list: List[List[Tuple[torch.Tensor, int]]]
    ):
        """Initialize Shampoo preconditioners and inverse preconditioners."""

        for group, buffer_ranks in zip(self.param_groups, buffer_ranks_list):
            preconditioner_count = 0
            for idx, p in enumerate(group[PARAMS]):
                state = self.state[p]
                dims = torch.as_tensor(p.shape)
                state[STEP] = torch.tensor(0)

                # Blocks the tensor and applies Shampoo to each block, with block
                # size equal to the max_preconditioner_dim; see feature above.
                if self._large_dim_method == LargeDimMethod.BLOCKING:
                    state[PRECONDITIONERS] = BlockShampooPreconditioner(
                        p,
                        beta2=group[BETAS][1],
                        epsilon=group[EPSILON],
                        exponent_override=self._exponent_override,
                        exponent_multiplier=self._exponent_multiplier,
                        use_bias_correction=self._use_bias_correction,
                        block_size=self._max_preconditioner_dim,
                        dtype=self._preconditioner_dtype,
                        idx=idx,
                        use_merge_dims=self._use_merge_dims,
                        start_preconditioning_step=self._start_preconditioning_step,
                        grafting_type=self._grafting_type,
                        grafting_beta2=self._grafting_beta2,
                        grafting_epsilon=self._grafting_epsilon,
                        group=self._dist_group,
                        dist_buffer_ranks=buffer_ranks,
                        dist_buffer_index=preconditioner_count,
                        use_protected_eigh=self._use_protected_eigh,
                        use_dtensor=self._use_dtensor,
                    )
                    preconditioner_count += len(
                        state[PRECONDITIONERS].get_split_dist_buffers()
                    )

                # Uses Adagrad preconditioner if any dimension is larger than
                # the max_preconditioner_dim; see features above.
                elif self._large_dim_method == LargeDimMethod.ADAGRAD:
                    dist_buffer, group_source_rank = (
                        buffer_ranks[preconditioner_count]
                        if buffer_ranks
                        else (None, 0)
                    )
                    preconditioner_count += 1
                    state[PRECONDITIONERS] = (
                        AdagradPreconditioner(
                            p,
                            beta2=group[BETAS][1],
                            epsilon=group[EPSILON],
                            use_bias_correction=self._use_bias_correction,
                            idx=idx,
                            group=self._dist_group,
                            group_source_rank=group_source_rank,
                            dist_buffer=dist_buffer,
                            use_dtensor=self._use_dtensor,
                        )
                        if torch.any(dims > self._max_preconditioner_dim)
                        else ShampooPreconditioner(
                            p,
                            beta2=group[BETAS][1],
                            epsilon=group[EPSILON],
                            exponent_override=self._exponent_override,
                            exponent_multiplier=self._exponent_multiplier,
                            use_bias_correction=self._use_bias_correction,
                            diagonal_threshold=self._max_preconditioner_dim,
                            dtype=self._preconditioner_dtype,
                            idx=idx,
                            start_preconditioning_step=self._start_preconditioning_step,
                            grafting_type=self._grafting_type,
                            grafting_beta2=self._grafting_beta2,
                            grafting_epsilon=self._grafting_epsilon,
                            group=self._dist_group,
                            group_source_rank=group_source_rank,
                            dist_buffer=dist_buffer,
                            use_protected_eigh=self._use_protected_eigh,
                            use_dtensor=self._use_dtensor,
                        )
                    )

                # Uses diagonal Shampoo preconditioner in place of full Shampoo
                # preconditioner if dimension is larger than max_preconditioner_dim; see feature
                # above.
                elif self._large_dim_method == LargeDimMethod.DIAGONAL:
                    dist_buffer, group_source_rank = (
                        buffer_ranks[preconditioner_count]
                        if buffer_ranks
                        else (None, 0)
                    )
                    preconditioner_count += 1
                    state[PRECONDITIONERS] = ShampooPreconditioner(
                        p,
                        beta2=group[BETAS][1],
                        epsilon=group[EPSILON],
                        exponent_override=self._exponent_override,
                        exponent_multiplier=self._exponent_multiplier,
                        use_bias_correction=self._use_bias_correction,
                        diagonal_threshold=self._max_preconditioner_dim,
                        dtype=self._preconditioner_dtype,
                        idx=idx,
                        start_preconditioning_step=self._start_preconditioning_step,
                        grafting_type=self._grafting_type,
                        grafting_beta2=self._grafting_beta2,
                        grafting_epsilon=self._grafting_epsilon,
                        group=self._dist_group,
                        group_source_rank=group_source_rank,
                        dist_buffer=dist_buffer,
                        use_protected_eigh=self._use_protected_eigh,
                        use_dtensor=self._use_dtensor,
                    )

                else:
                    raise ValueError(
                        "Large dim method "
                        + str(self._large_dim_method)
                        + " is not implemented!"
                    )

                # Count parameters from preconditioners for logging purposes.
                self._parameter_count += state[PRECONDITIONERS].parameter_count

        # Logs total number of parameters for optimizer.
        logger.info(f"Total Parameter Count: {self._parameter_count}")

    @torch.no_grad()
    def _assign_preconditioners_to_ranks(self) -> List[List[Tuple[torch.Tensor, int]]]:
        """Assign each preconditioner to a rank depending on strategy."""
        # Does not distribute computation.
        if not self._use_distributed():
            self._dist_group = None
            group_rank = 0

        # Distributes across default (global) process group.
        elif self._num_gpus_per_group == dist.get_world_size():
            self._dist_group = dist.distributed_c10d.GroupMember.WORLD
            group_rank = dist.get_rank()

        # Distributes across multiple process groups of equal size.
        # Currently supports only homogeneous architectures and assumes that the environmental
        # variable LOCAL_WORLD_SIZE is set (for example, through torchrun / torch.distributed.launch).
        else:
            # Creates different process groups for AllGather.
            # We need only one group, but we need to create multiple groups
            # as new_group() is a collective across all the processes.
            for group_ranks in [
                list(range(r, r + self._num_gpus_per_group))
                for r in range(0, dist.get_world_size(), self._num_gpus_per_group)
            ]:
                group = dist.new_group(ranks=group_ranks)

                # Determines which group this rank belongs to.
                if dist.get_rank() in group_ranks:
                    self._dist_group = group

            group_rank = dist.get_rank(group=self._dist_group)
            logger.info(
                f"Distributed Shampoo: Global Rank = {dist.get_rank()}, Group Rank = {group_rank}"
            )

        buffer_ranks_list = []

        # Calculate buffer sizes on a per-group basis.
        for group in self.param_groups:

            # Calculate necessary buffer sizes over all preconditioners for gradient communication.
            buffer_sizes = []
            for p in group[PARAMS]:
                if self._large_dim_method == LargeDimMethod.BLOCKING:
                    buffer_sizes.extend(
                        BlockShampooPreconditioner.get_dist_buffer_sizes(
                            p, self._max_preconditioner_dim, self._use_merge_dims
                        )
                    )

                else:
                    buffer_sizes.append(
                        DistributedPreconditioner.get_dist_buffer_size(p)
                    )

            # Calculate distribution across ranks using obtained buffer sizes.
            # buffer_size_ranks contains tuples of buffer sizes and ranks.
            buffer_size_ranks = distribute_buffer_sizes(
                buffer_sizes, self._num_gpus_per_group
            )

            # Allocate a single huge tensor.  Now every rank has the same size of buffer so
            # that we can use all_gather_into_tensor which performs the best in NCCL.
            # TODO: Switch from AllGather to AllGatherV once underlying NCCL supports efficient AllGatherV.
            max_buffer_size_sum = max(
                [
                    sum([s for s, r in buffer_size_ranks if r == group_rank])
                    for group_rank in range(self._num_gpus_per_group)
                ]
            )
            total_buffer_size = max_buffer_size_sum * self._num_gpus_per_group

            # global_dist_buffer allocated in terms of bytes.
            global_dist_buffer = torch.zeros(
                total_buffer_size,
                dtype=torch.int8,
                device=p.device,
            )
            group[DIST_BUFFER] = global_dist_buffer

            logger.info(
                f"Shampoo dist: group size = {self._num_gpus_per_group}, rank = {group_rank}, "
                + f"total data = {max_buffer_size_sum} [B], buffer size = {total_buffer_size} [B]"
            )

            # Split global_dist_buffer into as many local buffers as my_group_size
            local_dist_buffers = torch.split(global_dist_buffer, max_buffer_size_sum)
            group[MY_DIST_BUFFER] = local_dist_buffers[group_rank]

            # Further split local_dist_buffers so that we can assign them to preconditioners
            buffer_ranks_list.append(
                split_local_dist_buffers(buffer_size_ranks, local_dist_buffers)
            )

        return buffer_ranks_list

    @torch.no_grad()
    def _compute_root_inverse(self):
        """Root inverse computation across all preconditioners/parameters."""
        for group in self.param_groups:
            for p in group[PARAMS]:
                if p.grad is None:
                    continue

                state = self.state[p]

                if isinstance(
                    state[PRECONDITIONERS],
                    (ShampooPreconditioner, BlockShampooPreconditioner),
                ):
                    state[PRECONDITIONERS].compute_root_inverse()

    @torch.no_grad()
    def _compute_and_log_root_inverse_residuals(
        self,
    ):
        """Compute root inverse residuals over all preconditioners."""

        # Compute expected relative errors/residuals for debugging purposes
        if self._preconditioner_dtype == torch.float64:
            expected_relative_error = 1e-7
        elif self._preconditioner_dtype == torch.float:
            expected_relative_error = 1e-3
        else:
            logger.warning(
                "Expected relative error/residual not supported for precision lower than float32."
            )

        # Accumulate relative errors/residuals
        relative_errors = []
        relative_residuals = []

        for group in self.param_groups:
            for p in group[PARAMS]:
                state = self.state[p]

                if isinstance(
                    state[PRECONDITIONERS],
                    (ShampooPreconditioner, BlockShampooPreconditioner),
                ):
                    relative_error, relative_residual = state[
                        PRECONDITIONERS
                    ].compute_root_inverse_residuals()

                    relative_errors += relative_error
                    relative_residuals += relative_residual

        relative_errors = torch.stack(relative_errors)
        relative_residuals = torch.stack(relative_residuals)

        quantiles = torch.as_tensor(
            [0, 0.25, 0.5, 0.75, 1],
            device=relative_errors.device,
            dtype=relative_errors.dtype,
        )
        logger.debug(f"Expect Relative Error <= {expected_relative_error}")
        logger.debug(
            f"Relative Error (||X - X_hat||_inf / ||X||_inf)       Average: {torch.mean(relative_errors)}, Quantiles [0, 25, 50, 75, 100]: {torch.quantile(relative_errors, quantiles, interpolation='nearest')}"
        )
        logger.debug(
            f"Relative Residual (||X_hat^-r - A||_inf / ||A||_inf) Average: {torch.mean(relative_residuals)}, Quantiles [0, 25, 50, 75, 100]: {torch.quantile(relative_residuals, quantiles, interpolation='nearest')}"
        )

    @torch.no_grad()
    def _update_preconditioners(self):
        """Updates preconditioners.

        Note: If using L2-regularization/weight decay, it is computed within this function and
        therefore should not be recomputed elsewhere.

        """
        for group in self.param_groups:
            for p in group[PARAMS]:
                grad = p.grad
                state = self.state[p]
                if grad is None or not state[PRECONDITIONERS].on_source_rank:
                    continue

                weight_decay = group[WEIGHT_DECAY]

                # TODO: Sparse case still not supported.
                if p.grad.is_sparse:
                    raise Exception(
                        "Sparse parameters are not currently supported by Shampoo."
                    )

                else:
                    # Incorporate weight decay into the gradient
                    # if we are not using decoupled weight decay.
                    #
                    # Equivalent to adding an L2-regularization term:
                    #   F(w) + lambda * ||w||^2.
                    if not self._use_decoupled_weight_decay and weight_decay != 0:
                        grad.add_(p, alpha=weight_decay)

                    # Update each preconditioner using the gradient.
                    state[PRECONDITIONERS].update_preconditioners(grad, state[STEP])

    @torch.no_grad()
    def _init_group(
        self, group: Dict[str, Any]
    ) -> Tuple[List[torch.Tensor], List[torch.Tensor], List[torch.Tensor]]:
        # Set momentum parameter
        momentum_param = group[MOMENTUM]
        beta1, _ = group[BETAS]

        # Instantiate lists for params, grads, and momentum.
        split_params = []
        split_preconditioned_grads = []
        split_momentum_directions = []

        for p in group[PARAMS]:
            state = self.state[p]
            if p.grad is None:
                continue

            if p.grad.is_sparse:
                raise Exception(
                    "Sparse parameters are not currently supported by Shampoo."
                )

            # Initialize momentum and exponential moving average of gradient.
            if momentum_param != 0.0 and MOMENTUM not in state:
                state[MOMENTUM] = torch.zeros_like(
                    p.grad, memory_format=torch.preserve_format
                )
            if beta1 != 0.0 and EXP_AVG not in state:
                state[EXP_AVG] = torch.zeros_like(
                    p.grad, memory_format=torch.preserve_format
                )

            # Generate split lists.
            split_params.extend(state[PRECONDITIONERS].combine_and_split_dims(p))
            split_preconditioned_grads.extend(
                state[PRECONDITIONERS].get_split_dist_buffers()
            )
            split_momentum_directions.extend(
                state[PRECONDITIONERS].combine_and_split_dims(state[MOMENTUM])
                if momentum_param != 0.0
                else []
            )

        return split_params, split_preconditioned_grads, split_momentum_directions

    @torch.no_grad()
    def _iterate_step(self) -> torch.Tensor:
        for group in self.param_groups:
            for p in group[PARAMS]:
                self.state[p][STEP] += 1
        return self.state[p][STEP]

    @torch.no_grad()
    def reset_preconditioners(self):
        for group in self.param_groups:
            for p in group[PARAMS]:
                self.state[p][PRECONDITIONERS].reset_preconditioners()

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.

        Args:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.

        """
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        iteration = self._iterate_step()
        self._update_preconditioners()

        # Computes root inverse of all preconditioners every self._precondition_frequency
        # after the self._start_preconditioning_step iteration.
        if (
            iteration % self._precondition_frequency == 0
            and iteration >= self._start_preconditioning_step
        ):
            self._compute_root_inverse()

            if self._debug_mode:
                self._compute_and_log_root_inverse_residuals()

        # Loops over all parameter groups and parameters to perform update.
        for group in self.param_groups:
            split_params = []
            split_preconditioned_grads = []
            split_momentum_directions = []

            beta1, _ = group[BETAS]
            momentum_param = group[MOMENTUM]
            weight_decay = group[WEIGHT_DECAY]
            lr = group[LR]

            (
                split_params,
                split_preconditioned_grads,
                split_momentum_directions,
            ) = self._init_group(group)

            for p in group[PARAMS]:
                state = self.state[p]
                if p.grad is None or not state[PRECONDITIONERS].on_source_rank:
                    continue

                # Incorporate first-moment or filtered gradient estimation.
                if beta1 != 0:
                    # Compute bias corrections if necessary.
                    bias_correction1 = (
                        1.0 - beta1**iteration
                        if self._use_bias_correction
                        else torch.as_tensor(1.0)
                    )

                    # Compute exponential moving average of the gradient (with
                    # potential bias correction).
                    filtered_grad = state[EXP_AVG]
                    filtered_grad.mul_(beta1).add_(p.grad, alpha=1 - beta1)
                    p.grad.copy_(filtered_grad / bias_correction1)

                # Compute preconditioned gradient and update parameters.
                state[PRECONDITIONERS].preconditioned_grad_to_dist_buffer(
                    p.grad, iteration
                )

            # Perform all-gather to obtain search direction.
            if self._use_distributed():
                # Distribute preconditioned_grads that have been set to my_dist_buffer.
                dist.all_gather_into_tensor(
                    group[DIST_BUFFER],
                    group[MY_DIST_BUFFER],
                    group=self._dist_group,
                )

            # Set search direction as preconditioned grads.
            split_search_directions = split_preconditioned_grads

            # Incorporate decoupled weight decay.
            if self._use_decoupled_weight_decay and weight_decay != 0.0:
                # Decoupled weight decay (no momentum case)
                if momentum_param == 0.0:
                    torch._foreach_mul_(split_params, 1.0 - lr * weight_decay)

                # Decoupled weight decay (momentum case)
                else:
                    torch._foreach_add_(
                        split_search_directions, split_params, alpha=weight_decay
                    )

            # Update momentum.
            if momentum_param != 0.0:
                torch._foreach_mul_(split_momentum_directions, momentum_param)
                torch._foreach_add_(split_momentum_directions, split_search_directions)

                # Incorporates Nesterov momentum.
                if self._use_nesterov:
                    torch._foreach_add_(
                        split_search_directions,
                        split_momentum_directions,
                        alpha=momentum_param,
                    )

                else:
                    split_search_directions = split_momentum_directions

            # Updates weights.
            torch._foreach_add_(split_params, split_search_directions, alpha=-lr)

        return loss

    def load_state_dict(self, state_dict):
        r"""Loads the optimizer state.
        Args:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to :meth:`state_dict`.
        """
        # deepcopy, to be consistent with module API
        state_dict = deepcopy(state_dict)
        # Validate the state_dict
        groups = self.param_groups
        saved_groups = state_dict["param_groups"]

        if len(groups) != len(saved_groups):
            raise ValueError(
                "loaded state dict has a different number of parameter groups"
            )
        param_lens = (len(g[PARAMS]) for g in groups)
        saved_lens = (len(g[PARAMS]) for g in saved_groups)
        if any(p_len != s_len for p_len, s_len in zip(param_lens, saved_lens)):
            raise ValueError(
                "loaded state dict contains a parameter group "
                "that doesn't match the size of optimizer's group"
            )

        # Update the state
        id_map = {
            old_id: p
            for old_id, p in zip(
                chain.from_iterable((g[PARAMS] for g in saved_groups)),
                chain.from_iterable((g[PARAMS] for g in groups)),
            )
        }

        def cast(param, value, key=None):
            r"""Make a deep copy of value, casting all tensors to device of param."""
            if isinstance(value, torch.Tensor):
                return value
            elif isinstance(value, dict):
                return {k: cast(param, v, key=k) for k, v in value.items()}
            elif isinstance(value, container_abcs.Iterable):
                return type(value)(cast(param, v) for v in value)
            # Modified load_state_dict in order to ensure that the preconditioner objects
            # are casted correctly. This enables us to use generic map_locations when
            # checkpointing.
            elif isinstance(value, Preconditioner):
                value.to(param.device)
                return value
            else:
                return value

        # Copy state assigned to params (and cast tensors to appropriate types).
        # State that is not assigned to params is copied as is (needed for
        # backward compatibility).
        state = defaultdict(dict)
        for k, v in state_dict["state"].items():
            if k in id_map:
                param = id_map[k]
                state[param] = cast(param, v)
            else:
                state[k] = v

        # Update parameter groups, setting their 'params' value
        def update_group(group, new_group):
            new_group[PARAMS] = group[PARAMS]
            return new_group

        param_groups = [update_group(g, ng) for g, ng in zip(groups, saved_groups)]
        self.__setstate__({"state": state, "param_groups": param_groups})
