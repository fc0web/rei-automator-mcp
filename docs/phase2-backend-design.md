# Phase 2 — backend 選定 + 4 stub 実装設計

対象: `fc0web/rei-automator-mcp` @ c6696a8
担当: chat-Claude 設計 → Claude Code 実装

---

## 0. 先行修正 (Phase 2 より前)

### 0-1. allowlist bypass — 必須

`execute()` が allowlist を検査していない。`auto_approve=True` のとき
`propose()` が `approve()` を経由せず `approved` を立てるため、
allowlist 外の kind が実行される。**再現確認済み。**

```python
def execute(self, action_id: str) -> dict[str, Any]:
    action = self.pending.get(action_id)
    if action is None:
        return {"success": False, "result": "action not found", "dfumt": DFUMT_FALSE}

    # ★ 追加: 最終関門で allowlist を独立に検査する
    if action.kind not in self.allowed_actions:
        return {"success": False,
                "result": f"kind not allowed: {action.kind}",
                "dfumt": DFUMT_FALSE}

    if not action.approved:
        return {"success": False, "result": "not approved", "dfumt": DFUMT_NEITHER}
    ...
```

selftest に追加すべきケース:

```python
print("[5b] allowlist は auto_approve を貫通する")
d = Automator(data_dir=test_dir, allowed_actions=frozenset({"file_read"}),
              auto_approve=True)
aw2 = d.propose("shell_command", "should still be blocked", "echo PWNED")
ok(aw2.approved is True, "auto_approve により approved が立つ")
ok(d.execute(aw2.id)["success"] is False, "execute が allowlist で拒否する")
```

### 0-2. shell_command の encoding — 推奨

`cp932` 固定 decode は PowerShell 7 / 英語版 Windows で化ける。

```python
import locale
enc = locale.getpreferredencoding(False) or "utf-8"
out = r.stdout.decode(enc, errors="replace") if r.stdout else ""
```

---

## 1. backend 選定: pywinauto

canonical が Python 実行層に確定したため、pywinauto を採用する。
FlaUI (.NET) は pythonnet 層が必要になり、`mcp>=2.0.0` のみという
依存の軽さを崩す。

追加依存:

```
pywinauto>=0.6.9    # comtypes, pywin32, six を引き込む
Pillow>=10.0        # capture_as_image が要求
```

`pywinauto` は Windows 専用。`requirements.txt` を分けるか、
extras (`pip install .[windows]`) にするのが望ましい。

### 1-1. `uia` と `win32` の使い分け

pywinauto は 2 つの backend を持ち、**同じ API で挙動が違う**。

| | `win32` | `uia` |
|---|---|---|
| 基盤 | Win32 API + MSAA | UI Automation (UIA3) |
| 対象 | Win32 / MFC / 古い VB | WPF / WinForms / UWP / Qt / Electron |
| 速度 | 速い | 遅い (COM 越し、ツリー探索が重い) |
| ValuePattern | なし | あり ← **IME 回避の本命** |
| 取りこぼし | 新しい UI framework が見えない | 一部の古い Win32 で要素が潰れる |

**方針: `uia` を既定、`win32` を fallback。**

理由は `ValuePattern` です。これは `uia` にしかなく、
Phase 1.5 の IME 問題を構造的に消す唯一の経路。速度で `win32` に
劣りますが、失うものが大きすぎます。

`win32` に落ちるのは「`uia` で要素が見つからない」場合のみ。
判定は自動化せず、**action の command で明示指定できるように**して
おくこと (`backend=win32` オプション)。自動 fallback は
「どちらで動いたか分からない」状態を生み、再現性を壊します。

### 1-2. 接続コスト

`Application().connect()` は毎回コストがかかる。
`Desktop(backend="uia")` を module level で 1 個保持し、
プロセス寿命の間 使い回すこと。MCP サーバーは長寿命 stdio なので、
この前提が使えます。

---

## 2. 4 stub の実装設計

### 2-1. `screenshot` — pywinauto ではなく mss / Pillow

pywinauto の `capture_as_image()` はウィンドウハンドルが必要で、
「画面全体」には向かない。**この stub だけ pywinauto の管轄外。**

- 画面全体 / 指定領域 → `mss` (高速、依存が軽い) または `PIL.ImageGrab`
- 特定ウィンドウ → `window.capture_as_image()`

command 形式の提案:

```
screenshot  "full"                    → 全画面
screenshot  "region:0,0,800,600"      → 領域
screenshot  "window:メモ帳"            → ウィンドウ指定
```

保存先は `data_dir / "screenshots" / {action_id}.png`。
**戻り値にパスだけを返し、画像本体は返さないこと。**
MCP のレスポンスに base64 画像を載せると context を食い潰します。

### 2-2. `click` — 要素指定を第一、座標を fallback

現在の stub コメントは「UIA ClickablePoint pattern」ですが、
それは座標を取り出す手段であって、本筋は **要素の `Invoke`** です。

優先順:

1. `element.invoke()` — InvokePattern。座標もフォーカスも不要、最も堅い
2. `element.click_input()` — 実マウス移動。Invoke 非対応の要素向け
3. 座標クリック — 最後の手段

command 形式:

```
click  "title:保存"                   → 名前で探して Invoke
click  "auto_id:btnSave"              → AutomationId (最も安定)
click  "coord:640,480"                → 座標 (非推奨、記録に残す)
```

**`auto_id` を第一候補として推すこと。** 表示名は言語設定や
バージョンで変わりますが、AutomationId は開発者が付けた識別子なので
安定します。日本語環境で英語版アプリを操作する場合に特に効きます。

### 2-3. `search` — 仕様が未定義。実装前に決めること

4 つの stub のうち、**これだけ「何をするものか」が決まっていません。**
他の 3 つは対象が明確ですが、`search` は少なくとも 3 通りに読めます。

- (a) 画面上の UI 要素を探す (UIA ツリー検索)
- (b) ファイルシステムを検索
- (c) Web 検索

(a) なら `click` の下位機能として統合すべきで、独立した kind は不要。
(b) なら pywinauto は無関係。(c) は自動化ツールの責務ではない。

**推奨: (a) と解釈して `find_element` に改名し、`click` / `type` の
共通基盤として実装する。** UI ツリーを返すだけの read-only action として
定義すれば、副作用がなく allowlist 上も安全な部類になります。
Playwright MCP の accessibility snapshot と同じ位置づけです。

戻り値は要素のリスト (name / auto_id / control_type / rect)。
**ツリー全体をダンプしないこと。** 深さと件数に上限を設けます。

### 2-4. `excel_aggregate` — UIA ではなく COM / openpyxl

Excel を UI ツリー経由で操作するのは筋が悪い。セル 1 個ごとに
UIA を往復することになり、遅く脆い。**2 経路に分岐させること。**

| 状況 | 手段 |
|---|---|
| ファイルが閉じている | `openpyxl` — 直接読み書き。Excel 不要、高速、確実 |
| Excel で開いている | `win32com.client.Dispatch("Excel.Application")` |

既定は `openpyxl`。COM は「開いているブックに触る必要がある」場合のみ。

COM を使う場合の注意:
- `DispatchEx` ではなく `GetActiveObject` で既存インスタンスに繋ぐ
- `DisplayAlerts = False` を立てると、上書き確認が消える代わりに
  **警告なしで破壊する**。ユーザーの開いているブックを扱うので、
  ここは既定 False にしないこと
- COM は例外時に Excel プロセスが残る。`try/finally` で解放する

---

## 3. `type` の共存ロジック — Phase 1.5 と UIA

同じ仕事をする経路が 2 本になるため、優先順と fallback を決める。

### 3-1. 優先順

```
1. UIA ValuePattern.SetValue()   ← 対象要素が特定でき、対応している場合
2. SendInput + PS1 (Phase 1.5)   ← それ以外すべて
```

`SetValue` の利点は決定的です。キーストロークが発生しないので
IME を通らず、フォーカスも奪わない。ユーザーが別の作業をしていても
割り込みません (rank 3 の「focus 非奪取」の先取りになります)。

### 3-2. SetValue が使えない / 使ってはいけない場合

**必ず fallback すること:**

- 要素が `ValuePattern` 非対応 (`is_editable()` が False)
- 要素が read-only
- 要素を特定できない (command に対象指定がない)

**動いたように見えて動いていない場合があるため、検証が必須:**

`SetValue` は値を直接書き込むだけで、キー入力イベントを発火しません。
Electron / React 系のテキスト欄や、一部の WPF データバインディングでは、
**UI 上は文字が入るのにアプリ側の状態が更新されない**ことがあります。

```python
element.set_edit_text(text)          # または ValuePattern.SetValue
if element.get_value() != text:      # 書き戻し確認
    return self._type_via_ps1(text)  # 一致しなければ fallback
```

書き戻しが一致しても状態が更新されていないケースは残りますが、
そこまでは検出できません。**この制約は README に明記すること。**

### 3-3. 意味の違い — append できない

`SetValue` は**内容を全置換**します。カーソル位置への挿入や追記は
できません。SendInput はキー入力なので追記になります。

つまり 2 経路は「速度が違うだけの同じ操作」ではなく、
**振る舞いが違う別の操作**です。混同すると、fallback が発生した
瞬間に結果が変わります。

**推奨: `type` の command に mode を持たせて明示する。**

```
type  "replace::こんにちは"   → SetValue 優先、失敗時 全選択+SendInput
type  "append::こんにちは"    → SendInput のみ (SetValue は使わない)
```

既定を `append` にすると、既存の Phase 1.5 の挙動と一致するので
後方互換が保てます。

---

## 4. 実装順序の提案

1. **0-1 allowlist 修正** — public repo なので最優先、単独 commit
2. `find_element` (旧 `search`) — 他 2 つの基盤になる、副作用なし
3. `click` — `find_element` の上に乗る
4. `type` の共存ロジック — `find_element` が要る
5. `screenshot` — 独立、いつでもよい
6. `excel_aggregate` — 独立、COM の扱いが重いので最後

2〜4 は依存関係があるのでこの順。5 と 6 は並行可能です。
