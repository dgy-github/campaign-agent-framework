"""部署 / 隐私配置（M7）。对应框架文档：⑧部署层。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..core.models import ModelTier
from ..llm.client import TierConfig

DeployMode = Literal["local", "cloud", "hybrid"]


@dataclass
class Config:
    deploy_mode: DeployMode = "hybrid"
    privacy_strict: bool = True          # True: sensitive 任务强制走 local tier
    tiers: dict[ModelTier, TierConfig] = field(default_factory=dict)
    eval_threshold: float = 0.8

    def tier_for_sensitive(self) -> ModelTier:
        """敏感任务应使用的本地档位。

        规则：
        - 有 local=True 的 tier → 返回它
        - cloud 模式且 privacy_strict=True：抛出异常
        - 否则返回第一个可用 tier
        """
        # 先找 local=True 的 tier
        for tier_name, cfg in self.tiers.items():
            if cfg.local:
                return tier_name

        if self.deploy_mode == "cloud" and self.privacy_strict:
            raise ValueError(
                "Cannot process sensitive data in 'cloud' mode with privacy_strict=True. "
                "Switch to 'hybrid' or 'local' mode, or configure a local tier."
            )

        # cloud 无隐私约束 → 返回第一个可用 tier
        if self.tiers:
            return next(iter(self.tiers))

        raise ValueError("No tiers configured")

    def is_local_tier(self, tier: ModelTier) -> bool:
        """检查该 tier 是否为本地推理。"""
        cfg = self.tiers.get(tier)
        return cfg is not None and cfg.local
