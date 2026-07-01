"""Runtime patch for f2 subprocesses spawned by this app."""

try:
    from f2.apps.douyin.utils import TokenManager

    TokenManager.gen_real_msToken = classmethod(lambda cls: cls.gen_false_msToken())
except Exception:
    pass
