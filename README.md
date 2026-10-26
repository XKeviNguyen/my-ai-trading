<div align="center">

# 📈 My AI Trading
### Your market data. Your strategy. Your decision.

**Python-first · Evidence-driven · Multi-timeframe · AI-assisted · Read-only by default**

> *Đừng chỉ hỏi AI: “Có nên BUY?”*  
> *Hãy hỏi: “Dữ liệu đang nói gì, luận điểm nào sai, và điều gì sẽ khiến tôi thay đổi quyết định?”*

**🚧 PROJECT INCEPTION — Not a released product, not trading advice**  
**Started:** 26 October 2026 · **Current repo stage:** documentation-first / local development planned

</div>

---

## 🌠 Tầm nhìn

**My AI Trading** là nền tảng nghiên cứu và hỗ trợ ra quyết định giao dịch, có thể quan sát thị trường chủ động và trò chuyện với người dùng như một *Trading Copilot*. Không phải bot bán tín hiệu thần kỳ, không sao chép hệ thống bí mật của các trader nổi tiếng, và **không tự động đặt lệnh trong những phiên bản đầu**.

Hình dung: bạn đang xem **SANDUSDT Perpetual** trên Binance. Ứng dụng nhận diện đúng symbol và loại thị trường, tải OHLCV chuẩn từ API, so sánh các khung **1H / 4H / 1D / 1W**, rồi báo:

> **Đã phát hiện breakout với volume bất thường.** Daily cải thiện, nhưng weekly vẫn có rủi ro xu hướng giảm. Minervini: *chưa có VCP hoàn chỉnh*. Brandt: *chưa xác nhận đảo chiều dài hạn*. B.N.F.-inspired: *quan sát phản ứng tại hỗ trợ và timing ngắn hạn*.  
> Mỗi luận điểm phải kèm **dữ liệu, nến/timeframe, điều kiện xác nhận, invalidation và thời điểm quan sát**.

Đó chỉ là *ví dụ UX*, **không phải tín hiệu live hay khuyến nghị đầu tư**.

### Hai chế độ hoạt động đồng thời

| Chế độ | Vai trò | Khi nào hoạt động |
|---|---|---|
| **🛰️ Auto Analyst** | Tự quan sát ticker/watchlist, phát hiện sự kiện, cập nhật trạng thái, cảnh báo có kiểm soát | Khi dữ liệu mới hoặc một quy tắc có trạng thái thay đổi |
| **🧠 Trading Copilot** | Trò chuyện, kiểm tra giả thuyết, đối chiếu nhiều chiến lược, giải thích rủi ro | Khi người dùng đặt câu hỏi / yêu cầu phân tích sâu |

## 🧭 Nguyên tắc sản phẩm

1. **User owns the strategy:** chọn 0, 1, nhiều trader preset; kết hợp hoặc không dùng preset nào; tạo một hay nhiều custom strategies bằng prompt.
2. **Deterministic first:** Python tính giá/MA/ATR/volume/pivots và các điều kiện định lượng. Không bắt LLM “đoán” những gì có thể tính chính xác.
3. **AI on demand and on events:** Decision API / small LLM cho tác vụ phù hợp; vision và deep reasoning khi thật sự thêm giá trị, không gửi ảnh nến mỗi giây.
4. **Multiple timeframes, one source of truth:** có trạng thái candle **đã đóng/chưa đóng**, timezone, loại thị trường, nguồn dữ liệu, thời gian cập nhật, đồng bộ và kiểm soát stale data.
5. **Evidence before confidence:** kết quả có căn cứ, có mức độ bất định và điều kiện vô hiệu hóa; không suy diễn “90% confidence” thành “90% xác suất thắng”.
6. **Safe by default:** read-only, không dùng production trading keys, không tự giao dịch, không tự deploy, không tự merge.
7. **Cost is a product feature:** đo chi phí theo request, user, ticker, strategy, model; chia sẻ các phép tính công khai, cache an toàn, áp ngân sách và fallback.

## 🧪 Trạng thái thực tế — đừng nhầm kế hoạch với code đã có

| Hạng mục | Tình trạng (2026-10-26, nhánh `feature/fast-engine`, chưa commit) |
|---|---|
| Tài liệu định hướng, workflow cho Codex/Claude | **Có** |
| Python Fast Engine (nến đóng, gap, indicators, events có invalidation) | **Đã cài đặt + test offline** |
| Binance public adapter (perpetual + spot), cache, rate-limit | **Đã cài đặt + test bằng mock**; *chưa gọi Binance thật* |
| 20 methodology profiles + applicability gate (Native/Hybrid/Adapted) | **Catalog đủ 20**; 4 evaluator *experimental* (Minervini một phần, Weinstein, Turtle, Darvas); 16 còn lại *catalog only* |
| Custom rule DSL + compiler ngôn ngữ tự nhiên + phê duyệt theo `rule_hash` | **Đã cài đặt + test** |
| Auto Analyst monitor (cadence theo timeframe, chống trùng alert) | **Đã cài đặt + test offline** |
| Challenge My Thesis, Copilot router, budget/consent | **Đã cài đặt**; adapter AI *chưa gọi API thật* |
| Backtest (phí, slippage, funding, walk-forward) + ML baseline | **Đã cài đặt + test**; kết quả luôn là *HYPOTHETICAL* |
| Local API (token, Host allowlist) + CLI | **Đã cài đặt + test** |
| Chrome extension (Side Panel, TypeScript MV3) | **Đã cài đặt + test** (33 unit tests, chạy thử trong headless Chrome với dữ liệu giả); xem [`docs/EXTENSION.md`](docs/EXTENSION.md) |
| Live trading | **Ngoài phạm vi** — không bao giờ đặt lệnh |

Chi tiết tiến độ, cách chạy và việc tiếp theo: [`docs/PROGRESS.md`](docs/PROGRESS.md).

**Lưu ý:** scaffold V0.1 đã được audit trong thư mục cô lập (ngoài repo) ngày 2026-10-26: **16/16 offline/mocked tests pass trên Python 3.14.4** (FastAPI 0.162, Pydantic 2.14). Điều đó **không chứng minh** nó chạy được với Binance live hoặc có lợi thế giao dịch. Kết quả audit và kế hoạch tích hợp: [`docs/audits/V0.1_AUDIT.md`](docs/audits/V0.1_AUDIT.md).

**Yêu cầu sản phẩm chuẩn (authoritative):** [`docs/PRODUCT_REQUIREMENTS.md`](docs/PRODUCT_REQUIREMENTS.md) — PRD v1.1. README chỉ tóm tắt; khi mâu thuẫn, PRD được ưu tiên.

## ⏱️ Timeframe integrity & methodology fidelity (PRD F03 · F12 · F13)

**1H/4H/1D/1W là các *data view* có sẵn, không phải timeframe chung cho mọi chiến lược.** Mỗi phân tích tách bốn vai trò:

| Vai trò | Ý nghĩa |
|---|---|
| **Analysis timeframe** | Khung người dùng đang xem (ví dụ 1H) |
| **Methodology / native timeframe** | Khung mà phương pháp đã công bố thường được đánh giá (ví dụ Weinstein: weekly) |
| **Context timeframe** | Bối cảnh xu hướng/regime rộng hơn |
| **Execution timeframe** | (Tùy chọn) timing vào/ra lệnh |

Mỗi phiên bản chiến lược khai báo `timeframe_policy`: khung native đã được tài liệu hóa, lịch phiên/calendar, dữ liệu bắt buộc, khung execution tùy chọn, cách adaptation được phép và mức độ bằng chứng. Không rõ khung native → `UNVERIFIED`, **không bịa**.

**Không bao giờ âm thầm đổi `SMA(30, 1W)` thành `SMA(30, 1H)`** — 30 nến tuần và 30 nến giờ là hai thứ khác hẳn; MA 50 ngày không phải MA 50 giờ.

**Bốn chế độ phân tích** (bắt buộc gắn nhãn rõ cho người dùng):

| Mode | Ý nghĩa | Nhãn hiển thị |
|---|---|---|
| `NATIVE` | Quy tắc đã công bố, trên khung/horizon đã công bố, đủ dữ liệu | *Methodology-inspired / native scope* |
| `HYBRID` | Giữ bối cảnh native (weekly/fundamental/macro) + timing khung thấp do người dùng yêu cầu, nguồn riêng | *Hybrid: native context + user timing* |
| `ADAPTED` | Đổi tham số/khung/tài sản — fork phiên bản mới, opt-in, chỉ để nghiên cứu | *User-adapted — unvalidated; not the trader's original system* |
| `CUSTOM` | Chiến lược gốc của người dùng, không gán cho trader nào | *Custom strategy* |

**Trạng thái applicability** (mô tả *có áp dụng được không*, **không phải** tín hiệu mua/bán): `SUPPORTED_NATIVE`, `SUPPORTED_HYBRID`, `ADAPTED_UNVALIDATED`, `UNSUPPORTED_TIMEFRAME`, `UNSUPPORTED_ASSET`, `INSUFFICIENT_DATA`, `UNVERIFIED_METHOD`, `STALE_DATA`.

**Critical thinking (F12):** trợ lý phục vụ *chất lượng quyết định*, không chiều lòng người dùng. Mỗi luận điểm tách **quan sát / kết quả rule / diễn giải / giả định / điều chưa biết / phép thử đề xuất**, kèm bằng chứng ngược chiều và điều kiện vô hiệu hóa. Tính năng **Challenge My Thesis** đưa ra bull case và bear case độc lập. Không bỏ phiếu đa số giữa các trader; không bịa xác suất; LLM không được ghi đè cổng kiểm tra dữ liệu hay applicability.

**Kịch bản nghiệm thu G–L** (chi tiết: PRD §9, gắn vào milestone trong [`docs/ROADMAP.md`](docs/ROADMAP.md)):

- **G** — Weinstein trên chart 1H → đánh giá Stage Analysis trên 1W; SMA30 giờ không được gắn nhãn native.
- **H** — Peter Lynch trên SANDUSDT perpetual 1H → `UNSUPPORTED_ASSET` / `INSUFFICIENT_DATA`, không bao giờ có "Lynch hourly BUY".
- **I** — Người dùng biết rõ và vẫn muốn 1H → kết quả `HYBRID` / `ADAPTED` / `CUSTOM` có nhãn, không từ chối; bản gốc giữ nguyên.
- **J** — Breakout daily trong regime weekly giảm → Challenge My Thesis đưa cả hai phía, dữ liệu thiếu, điều kiện bác bỏ.
- **K** — Ba profile khác horizon/dữ liệu → báo applicability từng framework, không "2 BUY vs 1 SELL".
- **L** — Cổ phiếu theo phiên, crypto 24/7, nến đóng/chưa đóng → đúng calendar; không sinh tín hiệu weekly mới mỗi giờ.

## 🧰 Tech stack — dự kiến, không ép thêm dependency trước khi cần

- **Backend:** Python >= 3.11 (môi trường local hiện tại: 3.14.4; dùng `.venv` riêng của project, không sửa system Python), FastAPI (khi cần HTTP), Pydantic, `asyncio`/WebSocket.
- **Fast Engine:** trước hết sử dụng Python stdlib + phép tính kiểm thử được; sau đó cân nhắc NumPy/pandas khi có lợi ích rõ.
- **Market data:** public Binance USD-M Futures API cho nguyên mẫu; adapter độc lập nhà cung cấp, có quyền truy cập và licensing hợp lệ.
- **AI providers:** các adapter có version và budget cho small LLM, reasoning, multimodal vision, decision/classification providers. **Không ràng buộc core vào một vendor.**
- **Custom Strategy:** typed, validated rule AST/DSL; dịch từ natural language sang rule và yêu cầu xác thực trước khi chạy; **không chạy Python tùy ý từ prompt**.
- **Machine Learning (tùy giai đoạn):** scikit-learn/LightGBM để benchmark tín hiệu; PyTorch/PEFT/LoRA nếu fine-tuning có lợi ích được đo lường. Không thêm TensorFlow chỉ để “đủ stack”.
- **Vision (khi cần):** multimodal LLM + OpenCV/OCR cho ảnh; **ưu tiên OHLCV API** để lấy dữ liệu số chính xác.
- **Client:** ưu tiên Chrome Extension với giao diện TypeScript; lựa chọn desktop Python/PySide6 hoặc Tauri sẽ được benchmark/đánh giá riêng. Đầu tư vào core API tách biệt GUI.
- **Storage:** bắt đầu nhỏ (SQLite khi phù hợp), lên PostgreSQL khi yêu cầu multi-user, concurrency, scale chứng minh cần.
- **QA:** pytest, lint/type checks, CI, test offline/mock cho mọi thay đổi quan trọng.

### 🧠 20 phương pháp mặc định (library, không phải 20 AI bots luôn chạy)

| # | Trader | Lăng kính nghiên cứu |
|---:|---|---|
| 1 | Mark Minervini | SEPA, VCP, trend, breakout, volume |
| 2 | Peter Brandt | Classical price patterns, risk, invalidation |
| 3 | Jesse Livermore | Pivotal points, price leadership, risk |
| 4 | William J. O’Neil | CAN SLIM, earnings, accumulation |
| 5 | Stan Weinstein | Stage analysis, weekly trend |
| 6 | Nicolas Darvas | Darvas boxes, breakout |
| 7 | Kristjan Kullamägi | Momentum bursts, episodic pivots |
| 8 | Richard Dennis | Turtle trend following |
| 9 | Ed Seykota | Systematic trends, position sizing |
| 10 | Linda Bradford Raschke | Short-term setups, swing trading |
| 11 | Takashi Kotegawa (**B.N.F.**) | Short-term price reactions, momentum / mean reversion |
| 12 | **cis (しっさん)** | Trend/momentum, tactical management |
| 13 | Larry Williams | Short-term trading, volatility |
| 14 | Lance Breitstein | Intraday catalysts, risk management |
| 15 | Tom Hougaard | Price action, psychology |
| 16 | Paul Tudor Jones | Global macro, trading risk |
| 17 | George Soros | Macro, reflexivity |
| 18 | Stanley Druckenmiller | Macro themes, asymmetric opportunities |
| 19 | **Kelvin Chiu** | Commodities, relative-value, asymmetric trades |
| 20 | Peter Lynch | Business fundamentals, valuation, growth |

> **Research profiles, not replicas.** Đây là các bản tóm tắt mang tính giáo dục từ phương pháp công khai. Nhiều người không có bộ quy tắc cơ học được công bố đầy đủ. Không tự gán cho họ lệnh mua/bán hay tuyên bố được họ xác nhận. Chiến lược macro/fundamental/commodity-spread cần dữ liệu chuyên biệt; thiếu dữ liệu → `INSUFFICIENT_DATA`, không bịa ra nhận định từ chart crypto.

## 🏗️ Kiến trúc mục tiêu (phát triển tăng dần)

```text
Chart / Watchlist / User Question
         │
         ▼
[Symbol & Market Resolver] ── [Validated Market Data Adapters]
         │                         │
         └──────────► Closed-bar OHLCV + metadata
                                   │
                                   ▼
                [Shared Python Fast Engine]
              indicators / pivot / volume / patterns
                                   │
                      [Strategy Registry]
                20 presets + custom validated rules
                                   │
                [Event detection + dedup + budgets]
                     │                         │
                     ▼                         ▼
            [Auto Analyst]              [Trading Copilot]
                     └──────────┬──────────────┘
                                ▼
                     [AI Router, optional]
             Decision / small LLM / vision / reasoning
                                │
                                ▼
                    [Evidence & Audit Trail]
                                │
                                ▼
                      Alerts / Explanation UI
```

**Source of truth** cho các giá trị định lượng là dữ liệu chuẩn hóa + mã Python có test, không phải lời kể của LLM. Giữ market adapter, strategy logic, AI router và UI tách biệt để dễ thay thế, thu hẹp hoặc bổ sung provider.

## 💰 AI cost architecture

| Tầng | Điều kiện kích hoạt | Chi phí AI |
|---|---|---|
| **0. Python Fast Engine** | Mỗi bar/event hợp lệ | **$0 token**; vẫn có chi phí compute/network/storage |
| **1. Deterministic event filter** | Rule match, trạng thái thay đổi | $0 token |
| **2. Decision/small model** | Dữ liệu mơ hồ hoặc cần classification/summary | Có; chỉ gọi nếu vượt bài toán baseline |
| **3. Vision / deep reasoning** | Người dùng yêu cầu hoặc sự kiện quan trọng | Có; quota rõ ràng, cache đúng ranh giới |
| **4. Self-host / fine-tune** | Khi eval + economics chứng minh tốt hơn API | Có chi phí GPU, training, ops, latency |

Các biện pháp bắt buộc: batching/dedup/caching cho public computations; cô lập custom prompts, user data và secrets theo tenant; ghi token và **chi phí thực tế từ provider**; hard caps theo user / feature; không tin vào bảng giá lưu cứng từ tài liệu. Benchmark **accuracy, latency, cost per valid insight** bằng case có nhãn, không chỉ bằng quảng cáo của model.

## 🛣️ Roadmap có kiểm chứng

| Mốc | Giá trị bàn giao | Definition of Done |
|---|---|---|
| **R0 — Local-first repo** | README, hướng dẫn Codex/Claude, backlog, test plan | Clone/run docs được, không có secrets, chưa push code tự động |
| **R1 — Audit & import V0.1** | Kiểm tra lại scaffold ngoại vi, chỉnh packaging/tests, import có chọn lọc | Test sạch trên Ubuntu, tính toán trên nến đóng, network mock, không auth/trading |
| **R2 — Multi-timeframe analyst** | 1H/4H/1D/1W, đồng bộ closed bars, WebSocket resilience | Reconnect/dedup/stale-data tests, không dùng nến tương lai |
| **R3 — Strategy Studio** | 20 hồ sơ minh bạch về trạng thái + `timeframe_policy` + nhãn Native/Hybrid/Adapted/Custom; rules DSL; custom prompt compiler | Unknown criteria flagged, sandboxed/validated, reproducible; Scenarios G, H, I, K, L pass |
| **R4 — Trading Copilot + AI routing** | Explainable chat + Challenge My Thesis + optional vision/reasoning + budget monitor | Citations to market snapshot, measured per-call cost, abstain on insufficient data; Scenarios J, K pass |
| **R5 — Client & alerts** | Browser side panel hoặc desktop, notifications, watchlist | No secret leakage, low-resource and usability tests |
| **R6 — Research lab** | Event replay, paper signals, backtest/walk-forward | Fees/slippage/no leakage, no fake profitability claims |

**Không ghép tất cả thành một PR.** Mỗi mốc nhỏ, kiểm tra được, có reviewer đọc độc lập khi phù hợp. Xem [`docs/ROADMAP.md`](docs/ROADMAP.md).

## 🐧 Ubuntu + VS Code: bắt đầu như thế nào?

**Tác giả tự làm việc local bằng Codex/Claude Code rồi mới quyết định commit/push.** Trong ChatGPT, dự án này mới tạo bộ tài liệu để import; không chỉnh repo local của bạn và chưa tạo repository GitHub từ phiên chat này.

### A. Tạo repository trên GitHub (lần đầu, riêng tư mặc định)

- Tên GitHub repository nên là **`my-ai-trading`**; tiêu đề hiển thị trong README là **My AI Trading**. Tên repo không dùng khoảng trắng.
- Nếu dùng giao diện GitHub, tạo repo dưới tài khoản **`XKeviNguyen`**, chọn **Private**, thêm README tùy chọn, rồi clone URL GitHub cấp.
- Hoặc nếu đã đăng nhập GitHub CLI trên Ubuntu:

```bash
gh auth login
gh repo create XKeviNguyen/my-ai-trading --private --description \
  "Python-first, evidence-driven AI trading research and copilot" --clone
cd my-ai-trading
```

> Nếu repo đã tạo bằng website trước đó, **đừng chạy lại `gh repo create`**. Dùng `git clone https://github.com/XKeviNguyen/my-ai-trading.git` thay thế.

### B. Đưa tài liệu starter vào repo local

Giải nén **`my-ai-trading-repo-kit-v3.zip`** ở một thư mục tạm, sau đó copy nội dung bên trong kit vào thư mục Git vừa clone (**không** copy đè thư mục `.git`). README trong kit là tài liệu chuẩn dự án.

```bash
# Minh họa: giả sử ZIP đang ở ~/Downloads và repo ở ~/Projects/my-ai-trading
mkdir -p ~/Projects/my-ai-trading-docs
unzip -q ~/Downloads/my-ai-trading-repo-kit-v3.zip -d ~/Projects/my-ai-trading-docs
cp -a ~/Projects/my-ai-trading-docs/my-ai-trading-repo-kit/. ~/Projects/my-ai-trading/
cd ~/Projects/my-ai-trading
code .
```

> Nếu clone ở đường dẫn khác, sửa hai đường dẫn tương ứng. Xem thay đổi với `git status` trước khi commit.

### Multi-agent: Codex + Claude có thể chạy song song, có ranh giới

**Có, hỗ trợ triển khai nhiều AI coding agents đồng thời**, nhưng chỉ khi công việc thực sự độc lập. Không cho hai agents cùng sửa một working tree. Người điều phối chia task, khóa phạm vi sở hữu file, dùng worktree/branch riêng, rồi kiểm thử và tích hợp từng phần. Scout và sáu Breakers mặc định **chỉ đọc, báo lỗi**, không sửa code. Người dùng giữ quyền push, merge, deploy và mọi hành động bên ngoài có rủi ro.

Đọc **[`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md)** để có ví dụ `git worktree`, task contract, approval gate, cost/token cap, review độc lập và cách kiểm soát xung đột. **R1** nên chỉ dùng một coding implementer + reviewers read-only; chưa cho chạy các worker tính năng song song khi interface chưa ổn định.

### C. Nhờ coding agent làm việc local

1. Mở VS Code, đọc `README.md`, `AGENTS.md`, `CLAUDE.md`, [`docs/SECURITY.md`](docs/SECURITY.md) và [`docs/AGENT_KICKOFF.md`](docs/AGENT_KICKOFF.md).
2. Yêu cầu Codex hoặc Claude Code **kiểm tra thực trạng trước**, báo phương án import/audit scaffold V0.1; không tự giả định nó đã nằm trong Git.
3. Làm từng feature branch: `feat/market-data`, `feat/multi-timeframe`, `feat/strategy-engine`...
4. Chạy test/lint/security check; reviewer đọc độc lập khi risk đáng kể.
5. **Bạn quyết định** commit, mở PR, merge và push. Không tự deploy hoặc dùng tài khoản giao dịch thật.

Ví dụ workflow sau khi có code và tests:

```bash
git switch -c feat/market-data
# Codex / Claude edits locally, then you review
python -m pytest

git status --short
git diff --check
git diff
# After review and your explicit decision:
git add <reviewed-files>
git commit -m "feat: add validated market data ingestion"
git push -u origin feat/market-data
```

`git add <reviewed-files>` là chỗ bạn ghi tên file thực sự đã xem; **không thay bằng `git add -A` một cách mù quáng**.

## 🔒 Chống lộ `.env`, API keys và secrets (BẮT BUỘC)

**Không chỉ dựa vào `.gitignore`.** Git có thể vẫn commit file từng được track hoặc bị ép `git add -f`; token cũng có thể bị dán nhầm vào file `.py`, `.md` và lịch sử commits. Xem quy trình chi tiết tại **[`docs/SECURITY.md`](docs/SECURITY.md)**.

```bash
# Chạy từ repository root, trước commit
python3 tools/secret_guard.py --staged
# Sau khi bạn tự kiểm tra nội dung hooks, có thể bật hooks CHỈ cho repo này:
git config --local core.hooksPath .githooks
# Kiểm tra index (KHÔNG in nội dung secrets):
git diff --cached --name-only
```

`pre-commit` kiểm tra index; `pre-push` kiểm tra các commit sẽ đẩy đi, kể cả secret đã bị xóa trong commit sau. Đây là lớp phòng vệ bổ sung, không thay thế Gitleaks/full-history scan và GitHub secret scanning/push protection. Các hooks không tự bật trên máy người dùng; agent không được tắt hook hoặc bypass bằng `--no-verify`.

## 🔐 Engineering doctrine & release safety

Áp dụng có chọn lọc **sáu Breakers**: Security, Data, Flow, Environment, Performance, Efficiency & Simplicity. Ưu tiên: security/correctness → clarity/maintainability → performance/cost. Có concurrency/race testing và human-controlled merge/deploy. Chi tiết: [`AGENTS.md`](AGENTS.md).

- Không commit secrets, API keys, `.env`, tài khoản, dữ liệu cá nhân hoặc ảnh chụp có mã bí mật.
- Không đụng private trading endpoints; không yêu cầu Binance trade permissions.
- Không cho LLM trực tiếp sinh và thực thi arbitrary Python/SQL/shell code.
- Không dùng LLM output làm bằng chứng backtest hoặc xác suất lợi nhuận.
- Test offline trước, sau đó test public endpoint có giới hạn request (khi người dùng cho phép).
- Không giả định “works on my machine” = ready for public release.

## 🤝 Góp ý và pháp lý

Dự án ở giai đoạn nghiên cứu. Không được xem kết quả là tư vấn tài chính cá nhân, bảo đảm lợi nhuận hoặc lời mời sử dụng đòn bẩy. Tài liệu về các trader chỉ tham khảo phương pháp được công bố. **Chưa chọn giấy phép mã nguồn**; không mặc nhiên coi đây là mã nguồn mở chỉ vì repository có thể được công khai sau này.

---

<div align="center">

**Build with evidence. Trade with discipline. Keep humans in control.**

</div>
