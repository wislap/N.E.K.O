"""
插件冻结/解冻机制

提供 __freezable__ 属性的序列化/反序列化支持。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import time

try:
    import ormsgpack as msgpack
    _USE_ORMSGPACK = True
except ImportError:
    import msgpack  # type: ignore
    _USE_ORMSGPACK = False

from plugin.settings import CHECKPOINT_PERSIST_MODE, CHECKPOINT_PERSIST_INTERVAL

if TYPE_CHECKING:
    from loguru import Logger as LoguruLogger


class FreezableCheckpoint:
    """管理 __freezable__ 属性的 checkpoint 和冻结状态"""
    
    # 支持序列化的类型
    SERIALIZABLE_TYPES = (str, int, float, bool, type(None), list, dict, tuple, bytes)
    
    def __init__(
        self,
        plugin_id: str,
        plugin_dir: Path,
        logger: Optional["LoguruLogger"] = None,
    ):
        self.plugin_id = plugin_id
        self.plugin_dir = Path(plugin_dir)
        self.logger = logger
        
        # checkpoint 文件路径
        self._checkpoint_path = self.plugin_dir / ".checkpoint"
        # 冻结状态文件路径
        self._frozen_state_path = self.plugin_dir / ".frozen_state"
        
        # 内存中的最新 checkpoint
        self._last_checkpoint: Optional[bytes] = None
        self._last_checkpoint_time: float = 0.0
        
        # checkpoint 计数器（用于 interval 模式）
        self._checkpoint_count: int = 0
    
    def _is_serializable(self, value: Any) -> bool:
        """检查值是否可序列化"""
        if isinstance(value, self.SERIALIZABLE_TYPES):
            if isinstance(value, dict):
                return all(
                    isinstance(k, str) and self._is_serializable(v)
                    for k, v in value.items()
                )
            if isinstance(value, (list, tuple)):
                return all(self._is_serializable(item) for item in value)
            return True
        return False
    
    def _serialize(self, data: Dict[str, Any]) -> bytes:
        """序列化数据"""
        if _USE_ORMSGPACK:
            return msgpack.packb(data)
        return msgpack.packb(data, use_bin_type=True)
    
    def _deserialize(self, data: bytes) -> Dict[str, Any]:
        """反序列化数据"""
        if _USE_ORMSGPACK:
            return msgpack.unpackb(data)
        return msgpack.unpackb(data, raw=False)
    
    def collect_freezable_attrs(
        self,
        instance: Any,
        freezable_keys: List[str],
    ) -> Dict[str, Any]:
        """从插件实例收集 __freezable__ 声明的属性"""
        snapshot = {}
        for key in freezable_keys:
            if not hasattr(instance, key):
                if self.logger:
                    self.logger.debug(
                        f"[Freeze] Attribute '{key}' not found in plugin {self.plugin_id}"
                    )
                continue
            
            value = getattr(instance, key)
            if self._is_serializable(value):
                snapshot[key] = value
            else:
                if self.logger:
                    self.logger.warning(
                        f"[Freeze] Attribute '{key}' is not serializable, skipping"
                    )
        return snapshot
    
    def restore_freezable_attrs(
        self,
        instance: Any,
        snapshot: Dict[str, Any],
    ) -> int:
        """将 snapshot 中的属性恢复到插件实例"""
        restored_count = 0
        for key, value in snapshot.items():
            try:
                setattr(instance, key, value)
                restored_count += 1
            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        f"[Freeze] Failed to restore attribute '{key}': {e}"
                    )
        return restored_count
    
    def checkpoint(
        self,
        instance: Any,
        freezable_keys: List[str],
    ) -> bool:
        """创建 checkpoint
        
        根据 CHECKPOINT_PERSIST_MODE 配置决定持久化行为：
        - "memory": 仅保存到内存（默认）
        - "interval": 每 N 次 checkpoint 后写一次磁盘
        - "always": 每次都写磁盘
        """
        try:
            snapshot = self.collect_freezable_attrs(instance, freezable_keys)
            if not snapshot:
                return True
            
            self._last_checkpoint = self._serialize(snapshot)
            self._last_checkpoint_time = time.time()
            self._checkpoint_count += 1
            
            # 根据配置决定是否写盘
            should_persist = False
            if CHECKPOINT_PERSIST_MODE == "always":
                should_persist = True
            elif CHECKPOINT_PERSIST_MODE == "interval":
                if self._checkpoint_count % CHECKPOINT_PERSIST_INTERVAL == 0:
                    should_persist = True
            # "memory" 模式不写盘
            
            if should_persist:
                self._persist_checkpoint()
            
            return True
        except Exception as e:
            if self.logger:
                self.logger.exception(f"[Freeze] Checkpoint failed: {e}")
            return False
    
    def _persist_checkpoint(self) -> bool:
        """将内存中的 checkpoint 持久化到磁盘"""
        if not self._last_checkpoint:
            return False
        try:
            self._checkpoint_path.write_bytes(self._last_checkpoint)
            if self.logger:
                self.logger.debug(
                    f"[Freeze] Persisted checkpoint: {len(self._last_checkpoint)} bytes"
                )
            return True
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[Freeze] Failed to persist checkpoint: {e}")
            return False
    
    def restore_from_checkpoint(
        self,
        instance: Any,
    ) -> bool:
        """从内存 checkpoint 恢复属性"""
        if not self._last_checkpoint:
            return False
        
        try:
            snapshot = self._deserialize(self._last_checkpoint)
            self.restore_freezable_attrs(instance, snapshot)
            return True
        except Exception as e:
            if self.logger:
                self.logger.exception(f"[Freeze] Restore from checkpoint failed: {e}")
            return False
    
    def save_frozen_state(
        self,
        instance: Any,
        freezable_keys: List[str],
    ) -> bool:
        """保存冻结状态到文件"""
        try:
            snapshot = self.collect_freezable_attrs(instance, freezable_keys)
            frozen_data = {
                "version": 1,
                "plugin_id": self.plugin_id,
                "frozen_at": time.time(),
                "data": snapshot,
            }
            
            data_bytes = self._serialize(frozen_data)
            self._frozen_state_path.write_bytes(data_bytes)
            
            if self.logger:
                self.logger.info(
                    f"[Freeze] Saved frozen state: {len(snapshot)} attributes, "
                    f"{len(data_bytes)} bytes"
                )
            return True
        except Exception as e:
            if self.logger:
                self.logger.exception(f"[Freeze] Save frozen state failed: {e}")
            return False
    
    def load_frozen_state(
        self,
        instance: Any,
    ) -> bool:
        """从文件加载冻结状态"""
        if not self._frozen_state_path.exists():
            if self.logger:
                self.logger.debug("[Freeze] No frozen state file found")
            return False
        
        try:
            data_bytes = self._frozen_state_path.read_bytes()
            frozen_data = self._deserialize(data_bytes)
            
            version = frozen_data.get("version", 0)
            if version != 1:
                if self.logger:
                    self.logger.warning(
                        f"[Freeze] Unknown frozen state version: {version}"
                    )
                return False
            
            snapshot = frozen_data.get("data", {})
            restored = self.restore_freezable_attrs(instance, snapshot)
            
            if self.logger:
                self.logger.info(
                    f"[Freeze] Restored frozen state: {restored} attributes"
                )
            return True
        except Exception as e:
            if self.logger:
                self.logger.exception(f"[Freeze] Load frozen state failed: {e}")
            return False
    
    def clear_frozen_state(self) -> bool:
        """清除冻结状态文件"""
        try:
            if self._frozen_state_path.exists():
                self._frozen_state_path.unlink()
            return True
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[Freeze] Clear frozen state failed: {e}")
            return False
    
    def has_frozen_state(self) -> bool:
        """检查是否有冻结状态"""
        return self._frozen_state_path.exists()
    
    def get_frozen_state_info(self) -> Optional[Dict[str, Any]]:
        """获取冻结状态信息（不加载数据）"""
        if not self._frozen_state_path.exists():
            return None
        
        try:
            data_bytes = self._frozen_state_path.read_bytes()
            frozen_data = self._deserialize(data_bytes)
            return {
                "version": frozen_data.get("version"),
                "plugin_id": frozen_data.get("plugin_id"),
                "frozen_at": frozen_data.get("frozen_at"),
                "data_keys": list(frozen_data.get("data", {}).keys()),
                "file_size": len(data_bytes),
            }
        except Exception:
            return None
