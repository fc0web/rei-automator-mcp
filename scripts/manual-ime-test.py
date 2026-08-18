#!/usr/bin/env python3
"""
rei-automator-mcp : IME バイパス 実地 GUI テスト script

selftest [10] section で cover した 静的 verify (PS1 primitives + Python 経路) の
subsequent step。 Notepad を 起動 → hwnd 取得 → SendInput 経由で 日本語文字列を
送信 → 目視で 「IME OFF 状態でも 日本語が 正しく入力される」 を verify する。

**Windows-only**。 pywinauto (uia backend) が必要。

Usage:
    # 基本 test (Notepad 起動 → デフォルト文字列送信 → 手動確認)
    python scripts/manual-ime-test.py

    # 送信文字列を明示
    python scripts/manual-ime-test.py --text "こんにちは 世界 🌱"

    # foreground guard test (target_hwnd 不一致で abort されることを verify)
    python scripts/manual-ime-test.py --mismatch-hwnd

    # dry run (Notepad 起動なし、 PS1 script 呼び出せることのみ確認)
    python scripts/manual-ime-test.py --dry-run

期待動作:
    - Notepad が 起動、 IME 状態に関わらず 指定文字列が そのまま入力される
    - 半角/全角切替 keystroke なしで 日本語 code point が直接注入される
    - --mismatch-hwnd では PS1 が exit 3 + FOREGROUND_MISMATCH を返し、
      Python 側は "type aborted [foreground_mismatch]: ..." を返す

制約:
    - Notepad UI が Windows 11 で classic Notepad と modern Notepad (Store 版) が
      混在。 modern 版は UWP のため window handle 検出方法が異なる (uia backend が
      吸収するが、 一部 case で hwnd 0 になる)。
    - 管理者権限 で起動された Notepad へは UIPI で送信不可 (ERROR_ACCESS_DENIED)。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Windows terminal (cp932) で 日本語 + emoji を print する時 の UnicodeEncodeError を 防ぐ。
# Python 3.7+ で reconfigure が使える。 grounded v0.1.1 で 判明した Windows cp932 pitfall と 同型。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def dry_run() -> int:
    """PS1 script が 呼び出せることのみ verify (Notepad 起動なし)。"""
    from rei_automator_mcp import PS1_PATH

    print("=== dry run (PS1 script 呼び出し検証、 Notepad 起動なし) ===\n")
    print(f"PS1 script path: {PS1_PATH}")
    print(f"PS1 exists     : {PS1_PATH.exists()}")

    if not PS1_PATH.exists():
        print("FAIL: PS1 script not bundled", file=sys.stderr)
        return 1

    # PowerShell 存在確認
    import shutil

    ps_exe = shutil.which("powershell.exe") or shutil.which("pwsh")
    print(f"PowerShell exe : {ps_exe}")
    if not ps_exe:
        print("FAIL: powershell.exe / pwsh not on PATH", file=sys.stderr)
        return 1

    print("\nOK: dry run passed (PS1 + PowerShell 両方 available)")
    return 0


def launch_notepad_and_find_hwnd(retry_sec: float = 3.0) -> int | None:
    """Notepad を 起動 → hwnd を取得。 見つからなければ None。"""
    print("\n--- Launching Notepad ---")
    subprocess.Popen(["notepad.exe"])

    # Notepad 起動待機 + hwnd polling
    deadline = time.time() + retry_sec
    hwnd: int | None = None
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            from pywinauto.findwindows import find_elements  # type: ignore[import-not-found]
        except ImportError:
            print(
                "FAIL: pywinauto not installed. Run: pip install \"pywinauto>=0.6.9\"",
                file=sys.stderr,
            )
            return None
        # classic Notepad = "Notepad" class, modern Notepad = various
        for cls in ("Notepad", "ApplicationFrameWindow"):
            try:
                found = find_elements(class_name=cls, backend="uia", top_level_only=True)
            except Exception:
                found = []
            for elem in found:
                try:
                    name = (getattr(elem, "name", "") or "").lower()
                    h = int(getattr(elem, "handle", 0) or 0)
                    if h and ("notepad" in name or "メモ帳" in name or cls == "Notepad"):
                        hwnd = h
                        break
                except Exception:
                    continue
            if hwnd:
                break
        if hwnd:
            break

    if hwnd:
        print(f"Notepad hwnd = {hwnd} (0x{hwnd:08x})")
    else:
        print("Notepad hwnd not found within retry window", file=sys.stderr)
    return hwnd


def send_via_ps1(
    text: str, target_hwnd: int | None, foreground_retry_ms: int = 500
) -> tuple[int, str, str]:
    """PS1 script を 直接呼び出し (Automator を 経由せず、 low-level verify)。"""
    import base64
    import shutil

    from rei_automator_mcp import PS1_PATH

    ps_exe = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not ps_exe:
        return (127, "", "powershell not found")

    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    args = [
        ps_exe, "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", str(PS1_PATH),
        "-Base64", b64,
    ]
    if target_hwnd is not None:
        args += ["-ExpectedHwnd", str(int(target_hwnd)),
                 "-ForegroundRetryMs", str(int(foreground_retry_ms))]

    r = subprocess.run(args, capture_output=True, timeout=30)
    out = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
    err = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    return (r.returncode, out, err)


def notepad_test(text: str, use_mismatch_hwnd: bool = False) -> int:
    """Notepad 起動 + hwnd 取得 + 日本語送信 + 結果表示。"""
    print("=== Notepad IME バイパス test ===\n")
    print(f"送信文字列: {text!r}\n")

    hwnd = launch_notepad_and_find_hwnd()
    if hwnd is None:
        print("SKIP: Notepad hwnd 取得失敗", file=sys.stderr)
        return 2

    # Focus Notepad (ForegroundWindow 化)
    print("\n--- Focusing Notepad ---")
    try:
        from pywinauto.application import Application  # type: ignore[import-not-found]

        app = Application(backend="uia").connect(handle=hwnd)
        app.window(handle=hwnd).set_focus()
    except Exception as e:
        print(f"WARN: focus failed ({e}); PS1 side foreground retry で 補正します")

    time.sleep(0.5)

    if use_mismatch_hwnd:
        # 意図的に 存在しない hwnd を渡す → foreground guard が abort するはず
        bad_hwnd = hwnd ^ 0xFFFF  # bit flip = ほぼ確実に別 window (or 存在しない)
        print(
            f"\n--- Sending with WRONG hwnd (guard test) ---"
            f"\n  target_hwnd={bad_hwnd} (bit-flipped)"
        )
        rc, out, err = send_via_ps1(text, target_hwnd=bad_hwnd, foreground_retry_ms=200)
        print(f"  exit code: {rc}")
        print(f"  stdout   : {out.strip()}")
        print(f"  stderr   : {err.strip()}")
        if rc == 3 and "FOREGROUND_MISMATCH" in err:
            print("\nOK: foreground guard abort works (exit 3 + FOREGROUND_MISMATCH)")
            return 0
        print("\nFAIL: guard should have aborted but got exit", rc)
        return 1

    print(f"\n--- Sending to Notepad hwnd={hwnd} ---")
    rc, out, err = send_via_ps1(text, target_hwnd=hwnd, foreground_retry_ms=500)
    print(f"  exit code: {rc}")
    print(f"  stdout   : {out.strip()}")
    if err:
        print(f"  stderr   : {err.strip()}")

    if rc == 0 and "OK sent=" in out:
        print(
            "\nOK: 送信完了。 Notepad を 目視で確認してください。"
            "\n  - 日本語が そのまま入力されているか"
            "\n  - IME 状態 (半角/全角) の影響を受けていないか"
            "\n  - 改行 (\\n) が Enter として送られているか"
        )
        return 0

    print(f"\nFAIL: 送信失敗 exit={rc}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="rei-automator-mcp IME バイパス 実地 GUI テスト"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Notepad 起動なし、 PS1 script 呼び出し可能性のみ確認")
    parser.add_argument("--text", default="テスト日本語 IME バイパス 🌱\n改行も送れる",
                        help="Notepad に送る文字列 (default: 日本語 + emoji + 改行)")
    parser.add_argument("--mismatch-hwnd", action="store_true",
                        help="意図的に不一致 hwnd で送信、 foreground guard の abort を verify")
    args = parser.parse_args()

    if args.dry_run:
        return dry_run()

    return notepad_test(args.text, use_mismatch_hwnd=args.mismatch_hwnd)


if __name__ == "__main__":
    raise SystemExit(main())
