import os
import subprocess
import sys
import builtins


def console_print(*values, **options):
    """Log không được làm dừng tác vụ khi debugger thay luồng console."""
    try:
        builtins.print(*values, **options)
    except UnicodeEncodeError:
        safe_values = [str(value).encode('ascii', errors='backslashreplace').decode('ascii')
                       for value in values]
        builtins.print(*safe_values, **options)


def configure_utf8_console():
    """Cho phép log tiếng Việt khi Windows chuyển stdout sang tệp hoặc debugger."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='backslashreplace')
            except (OSError, ValueError):
                pass


_ORIGINAL_POPEN = subprocess.Popen
_POLICY_INSTALLED = False


def install_hidden_subprocess_policy():
    """Keep FFmpeg/FFprobe child processes invisible inside the render worker.

    This changes only Windows process presentation. Commands, arguments, timing,
    stdout/stderr handling and the video/audio pipeline remain untouched.
    """
    global _POLICY_INSTALLED
    if _POLICY_INSTALLED or os.name != "nt":
        return

    def hidden_popen(*args, **kwargs):
        creationflags = int(kwargs.get("creationflags", 0))
        kwargs["creationflags"] = creationflags | subprocess.CREATE_NO_WINDOW
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        return _ORIGINAL_POPEN(*args, **kwargs)

    subprocess.Popen = hidden_popen
    _POLICY_INSTALLED = True
