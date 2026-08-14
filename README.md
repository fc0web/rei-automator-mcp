# rei-automator-mcp

Windows PC 自動化 (shell / file / UI / 業務) を **propose → approve → execute** の 3 段 lifecycle で 実行する MCP サーバー。

Claude Desktop / Cursor / Cline など、 MCP 対応 client から 直接 wire して 使えます。

**Version**: 0.1.0 (2026-08-15) — 初版、 Rei-AIOS `WorkspaceAutomator` からの 独立抽出。

**License**: v0.x は MIT (irrevocable)。 v1.0+ で AGPL-3.0 + commercial dual への 切替可能性 予告 (LICENSE 参照)。

---

## これは何か

Rei-AIOS 内部に すでに `WorkspaceAutomator` (TypeScript、 400 行、 12 action kinds、 execution-log 永続化) が 存在します。 但し 呼び出しは Rei-AIOS 内部 (port 7511 REST + MCP proxy) 経由のみで、 外部 MCP client (Claude Desktop 単独 / Cursor / Cline / VS Code MCP 拡張) から は 直接 wire できません。

本 package は その 壁を 取り除きます。 独立 MCP サーバーとして 起動し、 12 action kinds を stdio 経由で 提供します。 Rei-AIOS 側の `WorkspaceAutomator` と 実装は 独立していますが、 (a) 3 段 lifecycle (propose → approve → execute)、 (b) 12 action kinds、 (c) `execution-log.json` 永続化、 (d) D-FUMT₈ 8 値 応答 の 4 axis は semantic 互換です。

---

## 何を解決するのか

3 種類の 摩擦を 取り除きます。

**(1) 「AI が うっかり 破壊的 command を 走らせる」 事故の 防止。**
副作用のある action は 必ず propose (提案) → approve (承認) → execute (実行) の 3 段を 通ります。 propose だけでは 何も起きません。 承認された action のみ 実行され、 log に 記録されます。

**(2) 「何が いつ 起きたか わからない」 状態の 解消。**
実行は 全て `~/.rei-automator-mcp/execution-log.json` に 追記され、 次回起動時に load されます (STEP 1336 pattern、 restart で 履歴が 消えない)。 in-memory + on-disk 二重で 稼働 signal を 保持。

**(3) 「日本語入力が IME で 化ける」 pain の 構造的回避。**
`type` action は `rei-sendinput.ps1` (Phase 1.5) 経由で `SendInput` + `KEYEVENTF_UNICODE` を 発行します。 IME 層を bypass するため、 IME が ON でも OFF でも 同じ結果に なります。 AutoHotkey が 15 年 解けていない問題を、 短期対策として 回避しています。

---

## installation

```bash
cd C:\Users\user\rei-automator-mcp
pip install -r requirements.txt
```

Python 3.10+ 必要。 依存は `mcp>=2.0.0` のみ (Python 標準ライブラリ + PowerShell (Windows 標準) のみで 全 action 動作)。

## 稼働確認 (selftest、 実装なし で 可)

```bash
python rei_automator_mcp.py --selftest
```

18 test 全 PASS で 稼働 準備完了。 selftest は 分離 data dir (`~/.rei-automator-mcp-selftest/`) を 使うため 本番 log を 汚染しません。

---

## Claude Desktop への 接続

`claude_desktop_config.json` に 以下を 追記:

```json
{
  "mcpServers": {
    "rei-automator": {
      "command": "python",
      "args": [
        "-u",
        "C:\\Users\\user\\rei-automator-mcp\\rei_automator_mcp.py"
      ]
    }
  }
}
```

Windows Store 版 Claude Desktop の 場合、 **config は Roaming + Package sandbox の 二重 path** で 参照されるので 両方を sync してから 再起動が 必要です (詳細は Rei stack memory `feedback_windows_store_claude_desktop_config_dual_path_2026-08-15` 参照)。

---

## 提供する MCP tools (7 種)

| tool | 用途 | dfumt (成功時) |
|---|---|---|
| `propose_action(kind, label, command, source_module?)` | action 提案 (承認待ちに 追加) | FLOWING |
| `approve_action(action_id)` | 提案済み action の 承認 | TRUE |
| `execute_action(action_id)` | 承認済み action の 実行 (log 記録) | TRUE |
| `cancel_action(action_id)` | 提案済み action の 取消 | NEITHER |
| `list_pending()` | 未実行 action 一覧 | — |
| `list_executed(limit?)` | 実行済み action 一覧 (新しい順) | — |
| `get_status()` | 現状 (pending数 / executed数 / last_action / ps1_bundled) | — |

## 12 action kinds (Phase 1 MVP 実装状況)

| kind | 実装 | 備考 |
|---|---|---|
| `shell_command` | ✅ subprocess (Windows cp932 decode) | timeout 30 sec |
| `file_read` | ✅ Python 標準 | UTF-8、 最大 2000 文字 preview |
| `file_write` | ✅ Python 標準 | command 形式 `"path::content"` |
| `type` | ✅ **Phase 1.5 rei-sendinput.ps1 (IME bypass)** | Base64 経由、 UIPI 制約あり |
| `wait` | ✅ time.sleep | command は 秒数 (string) |
| `note_export` | ✅ markdown 書き込み | command 形式 `"path::title"` |
| `report` | ✅ markdown 書き込み | command 形式 `"path::title"` |
| `proof_run` | ✅ 登録のみ (実行は Rei stack defer) | — |
| `open` | ✅ `os.startfile` (Windows) | — |
| `screenshot` | ⏸ stub (Phase 2 accessibility API) | — |
| `click` | ⏸ stub (Phase 2 UIA ClickablePoint) | — |
| `search` | ⏸ stub (backend 選定 defer) | — |
| `excel_aggregate` | ⏸ stub (openpyxl or COM 連携 defer) | — |

---

## 使用例 (Claude Desktop chat から)

```
User: 今日の日付を meta.md に書き込んでください
Claude: [propose_action で kind=file_write, command="C:\\tmp\\meta.md::2026-08-15\n"]
        [approve_action で 承認 (user 側で MCP tool consent dialog に 応答)]
        [execute_action で 実行]
        書き込み完了: C:\tmp\meta.md (12 文字)
```

```
User: メモ帳を開いて 「こんにちは」 と 入力してください
Claude: [propose_action で kind=open, command="notepad.exe"]
        [approve/execute]
        [3 秒待つ propose→approve→execute (wait 3)]
        [propose_action で kind=type, command="こんにちは"]
        [approve/execute — rei-sendinput.ps1 経由で IME bypass 入力]
```

---

## honest scope

1. **Windows-first、 cross-platform は Phase 2+ 検討**。 現状 `powershell.exe` + `os.startfile` は Windows 前提。
2. **Phase 1 MVP は 8/12 実装、 4 stub** (screenshot / click / search / excel_aggregate)。 Phase 2 (accessibility API 統合) で 本実装。
3. **`type` は UIPI (elevated window) 送信不可**。 管理者権限 window への 入力は `SendInput` の 仕様上 不可 (`GetLastError == 5 (ERROR_ACCESS_DENIED)`)。 Phase 2 UIA でも 解決しません。
4. **novelty ゼロ**。 本 package は Rei-AIOS `WorkspaceAutomator` からの 独立抽出であり、 MCP wrapper の 6+ 事例が 既存 (AHK-MCP 系)。 差別化は propose→approve→execute lifecycle + logic-basis 記録 + STEP 1336 persistence pattern + D-FUMT₈ dfumtValue の 4 axis。
5. **Rei stack `WorkspaceAutomator` (TypeScript 側) との 実装 divergence**。 現状は 独立 実装 の 並存で、 semantic 互換は 保つが 完全 mirror ではない。 「同 domain 複数実装 wiring audit」 (STEP 1336 対策 4 段目) で 定期 verify 推奨。
6. **Phase 2 (accessibility API 統合) 予告**。 chat-Claude と 分担で `FlaUI or pywinauto or UIA 直叩き` の backend 選定中。

---

## roadmap

- **v0.1** (本 release) — 3 段 lifecycle + 12 action kinds (8 実装 + 4 stub) + 永続化 + PS1 bundle
- **v0.2** — Phase 2 accessibility API 統合 (screenshot / click / search 本実装)
- **v0.3** — AbortSignal + focus 非奪取 (Phase 3)
- **v0.4** — cross-platform (macOS / Linux) 検討
- **v1.0** — production ready + license trajectory 判断

---

## 関連

- Rei-AIOS `WorkspaceAutomator` (TypeScript、 rei-aios repo `src/workspace/rei-automator/workspace-automator.ts`)
- `rei-sendinput.ps1` (Phase 1.5、 chat-Claude 実装、 本 package に bundle 済)
- benchtop-mcp / rei-solver / rei-fpga (Rei stack の 独立 MCP project pattern)
- Rei stack memory (private): `project_rei_automator_revival_arc_close_2026-08-15` (STEP 1336 fix + 本 arc)
