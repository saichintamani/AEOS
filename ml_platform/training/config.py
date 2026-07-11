"""
ML Platform — Training Engine: Configuration
=============================================
All training decisions are captured in TrainingConfig.
No magic defaults live inside the engine — everything is explicit.
The config is serialised to JSON and stored in the experiment tracker
alongside every training run for full reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeviceType(str, Enum):
    CPU   = "cpu"
    GPU   = "cuda"
    MPS   = "mps"       # Apple Silicon
    AUTO  = "auto"      # engine resolves at runtime


class Precision(str, Enum):
    FP32  = "fp32"      # default
    FP16  = "fp16"      # mixed precision (AMP)
    BF16  = "bfloat16"  # bfloat16 (A100 / newer GPUs)
    INT8  = "int8"      # quantized inference only


class OptimizerType(str, Enum):
    ADAM      = "adam"
    ADAMW     = "adamw"
    SGD       = "sgd"
    RMSPROP   = "rmsprop"
    ADAGRAD   = "adagrad"
    LBFGS     = "lbfgs"


class SchedulerType(str, Enum):
    NONE            = "none"
    STEP            = "step"
    COSINE          = "cosine"
    COSINE_WARM     = "cosine_with_warmup"
    EXPONENTIAL     = "exponential"
    REDUCE_ON_PLATEAU = "reduce_on_plateau"
    ONE_CYCLE       = "one_cycle"


@dataclass
class EarlyStoppingConfig:
    enabled:   bool  = True
    monitor:   str   = "val_loss"          # metric key to watch
    patience:  int   = 5                   # epochs without improvement
    min_delta: float = 1e-4               # minimum change threshold
    mode:      str   = "min"              # "min" or "max"
    restore_best_weights: bool = True


@dataclass
class CheckpointConfig:
    enabled:        bool  = True
    directory:      str   = "checkpoints"
    save_best_only: bool  = True
    monitor:        str   = "val_loss"
    save_every_n_epochs: int = 1
    max_to_keep:    int   = 3             # keep only last N checkpoints


@dataclass
class OptimizerConfig:
    type:            OptimizerType  = OptimizerType.ADAM
    learning_rate:   float          = 1e-3
    weight_decay:    float          = 0.0
    momentum:        float          = 0.9    # SGD / RMSProp
    beta1:           float          = 0.9    # Adam
    beta2:           float          = 0.999  # Adam
    eps:             float          = 1e-8
    extra:           dict[str, Any] = field(default_factory=dict)


@dataclass
class SchedulerConfig:
    type:          SchedulerType  = SchedulerType.NONE
    step_size:     int            = 10      # StepLR
    gamma:         float          = 0.1    # StepLR / Exponential
    T_max:         int            = 100    # CosineAnnealingLR
    warmup_steps:  int            = 0
    patience:      int            = 5      # ReduceLROnPlateau
    factor:        float          = 0.5    # ReduceLROnPlateau
    extra:         dict[str, Any] = field(default_factory=dict)


@dataclass
class TrainingConfig:
    """
    The single source of truth for one training run.
    Passed to TrainingEngine.run() — never modified during training.
    """
    # ── Identity ───────────────────────────────────────────────────────────────
    experiment_name: str
    run_name:        str  = ""            # auto-generated if empty

    # ── Data ───────────────────────────────────────────────────────────────────
    dataset_id:      str  = ""
    train_split:     float = 0.8
    val_split:       float = 0.1
    test_split:      float = 0.1
    batch_size:      int   = 32
    num_workers:     int   = 4
    shuffle:         bool  = True
    seed:            int   = 42

    # ── Training loop ──────────────────────────────────────────────────────────
    max_epochs:      int   = 100
    steps_per_epoch: int   = -1          # -1 = auto (len(dataset) / batch_size)
    gradient_clip:   float = 0.0         # 0.0 = disabled
    accumulate_grad_steps: int = 1       # gradient accumulation

    # ── Hardware ───────────────────────────────────────────────────────────────
    device:          DeviceType  = DeviceType.AUTO
    precision:       Precision   = Precision.FP32
    num_gpus:        int         = 1

    # ── Sub-configs ────────────────────────────────────────────────────────────
    optimizer:       OptimizerConfig    = field(default_factory=OptimizerConfig)
    scheduler:       SchedulerConfig    = field(default_factory=SchedulerConfig)
    early_stopping:  EarlyStoppingConfig = field(default_factory=EarlyStoppingConfig)
    checkpoint:      CheckpointConfig   = field(default_factory=CheckpointConfig)

    # ── Logging ────────────────────────────────────────────────────────────────
    log_every_n_steps: int  = 10
    tags:              dict[str, str] = field(default_factory=dict)
    extra:             dict[str, Any] = field(default_factory=dict)
