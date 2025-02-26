# PyTorch Distributed Shampoo

Distributed Shampoo is a second-order optimizer in the Adagrad family of methods [1, 2]. It converges in fewer iterations or epochs at the cost of more compute and memory.

The key to tuning this optimizer is to balance accuracy, performance, and memory. This is discussed below.

Developers:
- Hao-Jun Michael Shi (Meta Platforms, Inc.)
- Tsung-Hsien Lee
- Shintaro Iwasaki (Meta Platforms, Inc.)
- Jose Gallego-Posada (MILA / Meta Platforms, Inc.)

with contributions and support from:
- Rohan Anil (Google)
- Yizi Gu (Meta Platforms, Inc.)
- Vineet Gupta (Google)
- Minhui Huang (Meta Platforms, Inc.)
- Zhijing Li (Meta Platforms, Inc.)
- Wanchao Liang (Meta Platforms, Inc.)
- Dheevatsa Mudigere (NVIDIA)
- Mike Rabbat (Meta Platforms, Inc.)
- Kaushik Rangadurai (Meta Platforms, Inc.)
- Xunnan (Shawn) Xu (Meta Platforms, Inc.)

This implementation is under development. Currently only supports dense parameters.

## Features

Key distinctives of this implementation include:
- Learning rate grafting [3]. Our version of grafting only grafts the second moment/diagonal preconditioner. Momentum/first moment updates are performed separate from grafting. Supports the methods:
    - SGD
    - Adagrad
    - RMSProp
    - Adam
    - Normalized Adagrad
    - Normalized RMSProp
    - Normalized Adam
- Supports both normal and AdamW weight decay.
- Incorporates exponential moving averaging (with or without bias correction) to the estimate the first moment (akin to Adam).
- Incorporates momentum and Nesterov acceleration.
- Distributes memory and computation across different GPUs for the data-parallel setting. Supports data-parallel multi-node, multi-GPU training using `torch.nn.parallel.DistributedDataParallel`. Broadcasts are performed using `torch.dist`.
- Offers different options to handle large-dimensional tensors, including:
    - Diagonalizing the Shampoo preconditioners.
    - Using standard diagonal Adagrad.
    - Blocking the tensor and applying Shampoo to each block.
- Offers multiple approaches for computing the root inverse, including:
    - Using symmetric eigendecomposition (used by default).
    - Coupled inverse Newton iteration [4].
- Choice of precision for preconditioner accumulation and root inverse computation.
- Merging of small dimensions.

## Requirements

We have tested this implementation on the following versions of PyTorch:

- PyTorch >= 1.13;
- Python >= 3.8;
- CUDA 11.3-11.4; 12.

If one wants to use `DTensor` which leads to memory savings, please set the hidden default `use_dtensor = True` under `allocate_distributed_tensor` in `shampoo_dist_utils.py`. (This is on by default.) Requires PyTorch 2 nightly build.

Note: We have observed known instabilities with the `torch.linalg.eigh` operator on CUDA 11.6-11.8, specifically for low-rank matrices, which may appear with using a small `start_preconditioning_step`. Please avoid these versions of CUDA if possible.

## How to Use

**Given a learning rate schedule for your previous base optimizer, we can replace the optimizer with Shampoo and "graft" from the learning rate schedule of the base method.**

A few notes on hyperparameters:

- Notice that Shampoo contains some new hyperparameters (`max_preconditioner_dim` and `precondition_frequency`) that are important for performance. We describe how to tune these below in the section on Hyperparameter Tuning.

- Here, `betas` refer to the hyperparameters used for the exponential moving average of the gradients and Shampoo preconditioners, while `grafting_beta2` corresponds to the `beta2` used specifically for exponential moving averaging of the grafted method. This is similar for `epsilon` and `grafting_epsilon`. As a first choice, we recommend setting `betas` equal to the previous `betas` and additionally setting `grafting_beta2` equal to `betas[1]`, and set `epsilon = 1e-12` and `grafting_epsilon` equal to the previous `epsilon`.

- We also distinguish between `beta1` and `momentum`. `beta1` corresponds to the EMA of the gradients (or gradient filtering), while `momentum` corresponds to the SGD momentum formula applied to the search direction.

- We allow for decoupled and coupled weight decay. If one sets `use_decoupled_weight_decay=True`, then you are enabling AdamW-style weight decay, while `use_decoupled_weight_decay=False` corresponds to the normal L2-regularization style weight decay.

### Example 1: [SGD](https://pytorch.org/docs/stable/generated/torch.optim.SGD.html) with Momentum

If we previously used the optimizer:
```
import torch
from torch.optim import SGD

model = instantiate_model()

optimizer = SGD(
    model.parameters(),
    lr=0.01,
    momentum=0.9,
    weight_decay=1e-05,
)
```
we would instead use:
```
import torch
from distributed_shampoo import DistributedShampoo
from shampoo_utils import GraftingType

model = instantiate_model()

optimizer = DistributedShampoo(
    model.parameters(),
    lr=0.001,
    betas=(0., 0.999),
    epsilon=1e-12,
    momentum=0.9,
    weight_decay=1e-05,
    max_preconditioner_dim=8192,
    precondition_frequency=100,
    grafting_type=GraftingType.SGD,
)
```


### Example 2: [Adam](https://pytorch.org/docs/stable/generated/torch.optim.Adam.html)

If we previously used the optimizer:
```
import torch
from torch.optim import Adam

model = instantiate_model()

optimizer = Adam(
    model.parameters(),
    lr=0.001,
    betas=(0.9, 0.999),
    eps=1e-08,
    weight_decay=1e-05,
)
```
we would instead use:
```
import torch
from distributed_shampoo import DistributedShampoo
from shampoo_utils import GraftingType

model = instantiate_model()

optimizer = DistributedShampoo(
    model.parameters(),
    lr=0.001,
    betas=(0.9, 0.999),
    epsilon=1e-12,
    weight_decay=1e-05,
    max_preconditioner_dim=8192,
    precondition_frequency=100,
    use_decoupled_weight_decay=False,
    grafting_type=GraftingType.ADAM,
    grafting_epsilon=1e-08,
    grafting_beta2=0.999,
)
```

### Example 3: [Adagrad](https://pytorch.org/docs/stable/generated/torch.optim.Adagrad.html)

If we previously used the optimizer:
```
import torch
from torch.optim import Adagrad

model = instantiate_model()

optimizer = Adagrad(
    model.parameters(),
    lr=0.01,
    eps=1e-10,
    weight_decay=1e-05,
)
```
we would instead use:
```
import torch
from distributed_shampoo import DistributedShampoo
from shampoo_utils import GraftingType

model = instantiate_model()

optimizer = DistributedShampoo(
    model.parameters(),
    lr=0.01,
    betas=(0., 1.0),
    epsilon=1e-12,
    weight_decay=1e-05,
    max_preconditioner_dim=8192,
    precondition_frequency=100,
    use_decoupled_weight_decay=False,
    grafting_type=GraftingType.ADAGRAD,
    grafting_epsilon=1e-10,
)
```

### Example 4: [AdamW](https://pytorch.org/docs/stable/generated/torch.optim.AdamW.html)

If we previously used the optimizer:
```
import torch
from torch.optim import AdamW

model = instantiate_model()

optimizer = AdamW(
    model.parameters(),
    lr=0.001,
    betas=(0.9, 0.999),
    eps=1e-08,
    weight_decay=1e-05,
)
```
we would instead use:
```
import torch
from distributed_shampoo import DistributedShampoo
from shampoo_utils import GraftingType

model = instantiate_model()

optimizer = DistributedShampoo(
    model.parameters(),
    lr=0.001,
    betas=(0.9, 0.999),
    epsilon=1e-12,
    weight_decay=1e-05,
    max_preconditioner_dim=8192,
    precondition_frequency=100,
    use_decoupled_weight_decay=True,
    grafting_type=GraftingType.ADAM,
    grafting_epsilon=1e-08,
    grafting_beta2=0.999,
)
```

## Hyperparameter Tuning

**We want to tune Shampoo to balance model quality, memory, and efficiency/performance by applying approximations to a "pure" version of Shampoo.**

This requires adjusting the hyperparameters `max_preconditioner_dim`, `precondition_frequency`, and `start_preconditioning_step`. The general approach is to start by using as close to a “pure” version of Shampoo as possible, then incorporate approximations to ensure that one obtains fast performance. A pure version of Shampoo would set `max_preconditioner_dim = 8192` and `precondition_frequency = 1`.

With the inclusion of learning rate grafting, we can extract a good learning rate schedule from your existing scheduler. Other techniques for preventing divergence (gradient clipping) may also be removed.

### Step-by-Step Guide

1. Start with a reasonable `max_preconditioner_dim` (i.e., 8192) and reduce the block size as necessary for memory and performance.

    * The maximum effective value of this hyperparameter is the maximum value of the products of each layer’s dimensions. For example, if we have a model with three layers where the first layer is 5x5x3x6, the second layer is 3x3x3x8, and the third layer is 216x5; the products of the first, second, and third layers’ dimensions are 5x5x3x6=450, 3x3x3x8=216, and 216x10=1080, respectively. In this example, 1080 is the maximum effective value of this hyperparameter, and any value greater than 1080 will perform the same as 1080.

    * The higher this value is, the better the model quality we expect.

    * There is a sweet spot in terms of performance - if the number is too small, the algorithm will slow down due to kernel latency. On the other hand, using too large of a value leads to slow matrix computations (i.e., matrix root inverses), which scale as $O(n^3)$ if $n$ is the dimension of the matrix. In our experience, using a `max_preconditioner_dim` between 1024 and 8192 is ideal for performance.

    * Memory varies depending on the order of the tensor. For vectors, increasing `max_preconditioner_dim` leads to increased memory costs, but for 3rd-order tensors (or higher), increasing `max_preconditioner_dim` leads to decreased memory costs. Blocked matrices yield a fixed memory cost regardless of `max_preconditioner_dim`.

    * For efficiency purposes, it is best to set this value as a multiple of 2.

    * The following is an example of setting `max_preconditioner_dim = 4096` with SGD grafting:
    ```
    optimizer = shampoo.DistributedShampoo(
        nn.parameters(),
        lr=0.01,
        betas=(0., 0.999),
        momentum=0.9,
        weight_decay=0.01,
        max_preconditioner_dim=4096,
        grafting_type=GraftingType.SGD,
    )
    ```

2. Use the smallest `precondition_frequency` (i.e., 1) and increase the precondition frequency.

    * This hyperparameter determines how frequently the preconditioner is computed. The smaller the value, the slower Shampoo becomes but with faster convergence. The goal is to find a value that balances convergence and speed.

    * It is normal to eventually set this hyperparameter on the order of hundreds or thousands. This is based primarily on the size of the network and the effective ratio between the cost of a single forward-backward pass + standard optimizer step to the cost of computing a series of matrix root inverses.

    * In practice, we have found that an upper bound to `precondition_frequency` is on the order of thousands. This approach will offer diminishing performance gains if the bottleneck is due to preconditioning, which is performed at every iteration.

    * The following is an example of setting `precondition_frequency = 100`:
    ```
    optimizer = shampoo.DistributedShampoo(
        nn.parameters(),
        lr=0.01,
        betas=(0., 0.999),
        momentum=0.9,
        weight_decay=0.01,
        precondition_frequency=100,
        grafting_type=GraftingType.SGD,
    )
    ```

3. Set `start_preconditioning_step` to be consistent with the precondition frequency.

    * This hyperparameter determines when to start using Shampoo. Prior to this, the optimizer will use the grafted method. This value should generally be set larger than or equal to `precondition_frequency` except when the precondition frequency is 1. By default, `start_preconditioning_step` is set equal to `precondition_frequency`.

    * If the `precondition_frequency = 1`, then set `start_preconditioning_step = 0` in order to use Shampoo from the start.

    * Following is an example of setting `start_preconditioning_step = 300`:
    ```
    optimizer = shampoo.DistributedShampoo(
        nn.parameters(),
        lr=0.01,
        betas=(0., 0.999),
        momentum=0.9,
        weight_decay=0.01,
        start_preconditioning_step=300,
        grafting_type=GraftingType.SGD,
    )
    ```

4. One can fine-tune the algorithm further by playing with other hyperparameters, including:

    * Learning rate (`lr`),
    * Epsilon regularization (`epsilon`),
    * EMA parameters (`betas`),
    * Exponent override and multipliers (`exponent_override`, `exponent_multiplier`).

Using an exponent override of 2 is ideal for fully-connected layers. For MTML models, we have found that the task weights often need to be re-tuned as Distributed Shampoo will better exploit certain imbalances between different task losses.

## References

1. [Shampoo: Preconditioned Stochastic Tensor Optimization](https://proceedings.mlr.press/v80/gupta18a/gupta18a.pdf ). Vineet Gupta, Tomer Koren, and Yoram Singer. International Conference on Machine Learning, 2018.
2. [Scalable Second-Order Optimization for Deep Learning](https://arxiv.org/pdf/2002.09018.pdf). Rohan Anil, Vineet Gupta, Tomer Koren, Kevin Regan, and Yoram Singer. Tech Report, 2021.
3. [Learning Rate Grafting: Transferability of Optimizer Tuning](https://openreview.net/pdf?id=FpKgG31Z_i9). Naman Agarwal, Rohan Anil, Elad Hazan, Tomer Koren, and Cyril Zhang. Tech Report, 2021.
4. [Functions of Matrices: Theory and Computation.](https://epubs.siam.org/doi/book/10.1137/1.9780898717778) Nicholas J. Higham. SIAM, 2008.
