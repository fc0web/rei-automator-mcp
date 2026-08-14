# rei-sendinput.ps1
# Rei-Automator : IME バイパス型テキスト入力
#
# SendKeys / IME に依存せず、Unicode コードポイントを直接注入する。
# IME が ON でも OFF でも同一の結果になる。
#
# 使い方 (Base64 経由 : 文字化けを構造的に回避):
#   powershell -NoProfile -ExecutionPolicy Bypass -File rei-sendinput.ps1 -Base64 "44GT44KT44Gr44Gh44Gv"
#
# 使い方 (直接文字列 : デバッグ用のみ。コマンドライン経由は encoding 事故の温床):
#   powershell -NoProfile -File rei-sendinput.ps1 -Text "こんにちは"

[CmdletBinding(DefaultParameterSetName = 'Base64')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Base64')]
    [string]$Base64,

    [Parameter(Mandatory = $true, ParameterSetName = 'Text')]
    [string]$Text,

    # 1文字ごとの遅延 (ms)。0 = 一括送出（最速・推奨）。
    # 一部の古い Win32 アプリが取りこぼす場合のみ 5〜10 を指定する。
    [int]$DelayMs = 0,

    # 送出開始までの待ち時間 (ms)。
    # SendInput は「現在フォーカスのあるウィンドウ」に入力する。
    # ターミナルから手動実行すると、フォーカスはターミナル自身にあるため
    # 文字がターミナルに入ってしまう。
    # 受け入れテスト時は 5000 程度を指定し、その間に対象ウィンドウ
    # (メモ帳等) をクリックしてフォーカスを移すこと。
    # TS から呼ぶ通常運用では 0 のまま。
    [int]$StartDelayMs = 0
)

$ErrorActionPreference = 'Stop'

if ($PSCmdlet.ParameterSetName -eq 'Base64') {
    $Text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Base64))
}

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class ReiSendInput
{
    // --- Win32 構造体 -------------------------------------------------
    // 注意: union のサイズを正しくするため MOUSEINPUT を必ず含めること。
    // KEYBDINPUT だけで union を作ると x64 で INPUT が 32 byte になり、
    // SendInput が cbSize 不一致で 0 を返して沈黙する。
    // 正しいサイズ: x86 = 28 byte / x64 = 40 byte。

    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT {
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct HARDWAREINPUT {
        public uint uMsg;
        public ushort wParamL;
        public ushort wParamH;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct InputUnion {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public HARDWAREINPUT hi;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {
        public uint type;
        public InputUnion u;
    }

    const uint INPUT_KEYBOARD    = 1;
    const uint KEYEVENTF_KEYUP   = 0x0002;
    const uint KEYEVENTF_UNICODE = 0x0004;
    const ushort VK_RETURN       = 0x0D;
    const ushort VK_TAB          = 0x09;

    [DllImport("user32.dll", SetLastError = true)]
    static extern uint SendInput(uint nInputs, [In] INPUT[] pInputs, int cbSize);

    // Unicode コードユニットを直接注入する INPUT を作る。
    // wVk = 0 が必須。KEYEVENTF_UNICODE 指定時に wVk が非0だと無視される。
    static INPUT Unicode(ushort codeUnit, bool keyUp) {
        INPUT i = new INPUT();
        i.type = INPUT_KEYBOARD;
        i.u.ki.wVk = 0;
        i.u.ki.wScan = codeUnit;
        i.u.ki.dwFlags = KEYEVENTF_UNICODE | (keyUp ? KEYEVENTF_KEYUP : 0);
        i.u.ki.time = 0;
        i.u.ki.dwExtraInfo = IntPtr.Zero;
        return i;
    }

    // 改行・タブは Unicode 注入では効かない。仮想キーとして送る。
    static INPUT VirtualKey(ushort vk, bool keyUp) {
        INPUT i = new INPUT();
        i.type = INPUT_KEYBOARD;
        i.u.ki.wVk = vk;
        i.u.ki.wScan = 0;
        i.u.ki.dwFlags = keyUp ? KEYEVENTF_KEYUP : 0;
        i.u.ki.time = 0;
        i.u.ki.dwExtraInfo = IntPtr.Zero;
        return i;
    }

    static void Flush(List<INPUT> buf) {
        if (buf.Count == 0) return;
        INPUT[] arr = buf.ToArray();
        int size = Marshal.SizeOf(typeof(INPUT));
        uint sent = SendInput((uint)arr.Length, arr, size);
        if (sent != (uint)arr.Length) {
            int err = Marshal.GetLastWin32Error();
            throw new Exception(
                "SendInput failed. sent=" + sent + " expected=" + arr.Length +
                " cbSize=" + size + " lastError=" + err +
                (err == 5 ? " (ERROR_ACCESS_DENIED: 対象ウィンドウが管理者権限で動作している可能性。UIPI により送出不可)" : ""));
        }
        buf.Clear();
    }

    public static int SendText(string text, int delayMs) {
        List<INPUT> buf = new List<INPUT>();
        int count = 0;

        foreach (char c in text) {
            if (c == '\r') continue;              // CRLF の CR は捨てる
            if (c == '\n') {
                buf.Add(VirtualKey(VK_RETURN, false));
                buf.Add(VirtualKey(VK_RETURN, true));
            } else if (c == '\t') {
                buf.Add(VirtualKey(VK_TAB, false));
                buf.Add(VirtualKey(VK_TAB, true));
            } else {
                // サロゲートペアは上位・下位を連続した INPUT として送る。
                // 日本語 (ひらがな/カタカナ/常用漢字) は全て BMP 内なので
                // ここは絵文字・一部異体字のみが対象。
                buf.Add(Unicode((ushort)c, false));
                buf.Add(Unicode((ushort)c, true));
            }
            count++;

            if (delayMs > 0) {
                Flush(buf);
                System.Threading.Thread.Sleep(delayMs);
            }
        }
        Flush(buf);
        return count;
    }

    // 診断用: 構造体サイズが正しいか確認する
    public static int StructSize() {
        return Marshal.SizeOf(typeof(INPUT));
    }
}
"@

$expected = if ([Environment]::Is64BitProcess) { 40 } else { 28 }
$actual = [ReiSendInput]::StructSize()
if ($actual -ne $expected) {
    throw "INPUT struct size mismatch: expected $expected, got $actual"
}

if ($StartDelayMs -gt 0) {
    $sec = [math]::Ceiling($StartDelayMs / 1000)
    Write-Host "対象ウィンドウをクリックしてフォーカスを移してください..." -ForegroundColor Yellow
    for ($i = $sec; $i -gt 0; $i--) {
        Write-Host "  $i" -ForegroundColor Yellow
        Start-Sleep -Milliseconds 1000
    }
}

$sent = [ReiSendInput]::SendText($Text, $DelayMs)
Write-Output "OK sent=$sent"
