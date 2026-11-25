# 保留 CircuitPython 安全模式，避免供电、USB 或持续异常造成无限掉盘重启。
import supervisor

print("[safe] reason:", supervisor.runtime.safe_mode_reason)
print("[safe] Inspect USB/power and serial logs; reset manually after fixing the cause.")
