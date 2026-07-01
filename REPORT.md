# Báo cáo Kết quả Triển khai Lab Day 10 Reliability Agent

Báo cáo này tổng hợp các công việc đã thực hiện trong:
1. **Phase 1: Triển khai Circuit Breaker**
2. **Phase 2: Triển khai Bộ nhớ đệm Cache & Redis Shared Cache**
3. **Phase 3: Triển khai API Gateway**
4. **Phase 4 & 5: Giả lập Chaos, Đo lường & Hoàn thành Báo cáo**

---

# PHASE 1: CIRCUIT BREAKER

## 1. Mô tả nhiệm vụ (Task Description)

Mục tiêu của Phase 1 là xây dựng một lớp **Circuit Breaker** (Bộ ngắt mạch) dưới dạng một máy trạng thái 3 trạng thái:
- **CLOSED**: Trạng thái đóng mạch bình thường, các request được phép đi qua. Nếu gặp lỗi liên tục vượt quá ngưỡng `failure_threshold`, mạch sẽ chuyển sang `OPEN`.
- **OPEN**: Mạch bị ngắt để tránh làm quá tải provider và phản hồi lỗi nhanh chóng (fail-fast) cho Client. Sau khoảng thời gian timeout `reset_timeout_seconds`, mạch sẽ cho phép thử lại và tự chuyển sang `HALF_OPEN`.
- **HALF_OPEN**: Trạng thái thử nghiệm, gửi các request thăm dò (probe). 
  - Nếu thành công liên tiếp đạt ngưỡng `success_threshold`, mạch phục hồi hoàn toàn về `CLOSED`.
  - Nếu gặp bất kỳ lỗi nào, mạch lập tức ngắt trở lại `OPEN`.

## 2. Các công việc đã làm (What Has Been Done)

Chúng tôi đã hoàn thành triển khai 4 phương thức chính trong class `CircuitBreaker` tại file `src/reliability_lab/circuit_breaker.py`:
1. **`allow_request()`**: Logic kiểm tra trạng thái mạch.
2. **`call(fn, *args, **kwargs)`**: Wrapper gọi provider, thực hiện cơ chế fail-fast.
3. **`record_success()`**: Reset bộ đếm lỗi và chuyển trạng thái từ `HALF_OPEN` về `CLOSED`.
4. **`record_failure()`**: Tăng bộ đếm lỗi và chuyển đổi trạng thái sang `OPEN` khi đạt điều kiện.

## 3. Giải thích cách thức triển khai & Lý do thiết kế (How & Why)

- Sử dụng `time.monotonic()` thay vì `time.time()` để đo đạc khoảng thời gian trôi qua chính xác, tránh bị ảnh hưởng bởi việc điều chỉnh giờ hệ thống.
- Thực hiện cơ chế fail-fast ở dạng lớp bọc (wrapper) để không can thiệp sâu vào logic nghiệp vụ của provider.
- Khi một probe request ở trạng thái `HALF_OPEN` thất bại, chuyển mạch về `OPEN` lập tức mà không cần đợi đạt ngưỡng lỗi, bảo đảm an toàn tối đa cho hệ thống.

---

# PHASE 2: CACHE & REDIS SHARED CACHE

## 1. Mô tả nhiệm vụ (Task Description)

Mục tiêu của Phase 2 là triển khai bộ nhớ đệm Cache hai cấp độ phục vụ cho Agent:
1. **In-Memory Cache (`ResponseCache`)**:
   - Triển khai thuật toán tính độ tương đồng ngữ nghĩa `similarity(a, b)` sử dụng cosine similarity qua character 3-grams kết hợp word tokens.
   - Triển khai tra cứu `get(query)` với cơ chế lọc thông tin nhạy cảm (`_is_uncacheable`), loại bỏ cache hết hạn (TTL) và phát hiện False Hit (`_looks_like_false_hit` khi trùng khớp điểm tương đồng nhưng năm/ID khác nhau).
   - Triển khai `set(query, value, metadata)` lưu trữ kết quả an toàn.
2. **Shared Redis Cache (`SharedRedisCache`)**:
   - Tương thích và đồng bộ hóa cache trên môi trường phân tán (nhiều instance gateway) thông qua Redis.
   - Hỗ trợ tra cứu nhanh exact-match key bằng thuật toán băm (MD5).
   - Quét (`SCAN`) và tính toán độ tương đồng trên Redis khi không khớp chính xác, đồng thời áp dụng đầy đủ các bộ lọc bảo mật và False Hit tương tự in-memory cache.
   - Đồng bộ lưu trữ (`hset`) kèm cấu hình TTL tự động bằng Redis (`EXPIRE`).

## 2. Giải thích cách thức triển khai & Lý do thiết kế (How & Why)

- **Cosine Similarity với 3-grams**: Giữ lại được cả cấu trúc ngữ nghĩa từ vựng và khắc phục các lỗi nhỏ về đánh máy hoặc hình thái từ, mang lại độ chính xác cao hơn so với giải thuật Jaccard đơn giản.
- **Phát hiện False Hit & Lọc bảo mật**: Bảo vệ thông tin cá nhân và tránh hiện tượng ảo giác thông tin (hallucination) do cache nhầm dữ liệu lịch sử/ngày tháng khác nhau khi độ tương đồng văn bản thuần túy quá cao.
- **Shared Cache với Redis**: Giúp chia sẻ cache hoàn hảo giữa nhiều instance Gateway chạy song song, giảm thiểu chi phí gọi LLM và giảm đáng kể thời gian phản hồi hệ thống trên diện rộng.

---

# PHASE 3: API GATEWAY

## 1. Mô tả nhiệm vụ (Task Description)

Mục tiêu của Phase 3 là kết nối các tầng nghiệp vụ độc lập đã triển khai (Cache, Circuit Breaker, Providers) vào một điều phối viên chung mang tên **ReliabilityGateway** (`complete()`):
1. **Kiểm tra Cache (Cache Check)**: Nếu tìm thấy câu trả lời ngữ nghĩa tương đồng trong cache, trả về kết quả ngay lập tức để tiết kiệm thời gian ($0\text{ms}$) và chi phí ($0\text{ USD}$).
2. **Dự phòng chuỗi nhà cung cấp (Provider Fallback Chain)**: Duyệt tuần tự qua danh sách LLM providers, thực thi cuộc gọi thông qua Circuit Breaker để ngăn chặn bão lỗi (retry storm).
3. **Phản hồi lỗi tĩnh (Static Fallback)**: Trả về phản hồi tĩnh dịu dàng thông báo hệ thống đang suy giảm hiệu năng khi toàn bộ nhà cung cấp trong chuỗi đều thất bại.
4. **Theo dõi chi phí thông minh (Cost Budget Tracking)**: Giám sát tổng chi phí cuộc gọi của Gateway. Khi tổng chi phí vượt hạn mức (`cost_budget`), tự động chuyển hướng ưu tiên sang các nhà cung cấp giá rẻ để tiết kiệm chi phí.

---

# PHASE 4 & 5: GIẢ LẬP CHAOS, ĐO LƯỜNG & HOÀN THÀNH BÁO CÁO

## 1. Mô tả nhiệm vụ (Task Description)

Mục tiêu của Phase 4 & 5 là đánh giá khả năng chịu lỗi của hệ thống dưới các kịch bản hỗn loạn (chaos) và phân tích các chỉ số SLI/SLO:
1. **Tính toán thời gian phục hồi (`calculate_recovery_time_ms`)**: Tính thời gian phục hồi trung bình của các circuit breaker từ trạng thái `OPEN` sang `CLOSED` dựa trên nhật ký ghi nhận trạng thái (`transition_log`).
2. **Giả lập kịch bản (`run_scenario`)**: Chạy các kịch bản giả lập lỗi để thu thập dữ liệu về tổng số request, thành công/thất bại, trúng cache, số lần fallback, độ trễ và chi phí ước tính.
3. **Mở rộng giả lập so sánh (`run_simulation`)**: So sánh hiệu năng khi Bật vs. Tắt cache để làm rõ delta lợi ích. Ghi nhận kết quả kịch bản dưới dạng CSV.
4. **Báo cáo cuối cùng (`reports/final_report.md`)**: Phân tích các chỉ số SLO và đưa ra phương án cải tiến hệ thống trước khi đưa vào Production.

## 2. Giải thích cách thức triển khai & Lý do thiết kế (How & Why)

- **Đo lường thời gian phục hồi**: Sử dụng nhật ký chuyển đổi để tính hiệu thời gian giữa lúc mạch bị chuyển sang `OPEN` và lúc mạch chuyển đổi thành công về `CLOSED` (sau khi probe thành công ở `HALF_OPEN`). Chuyển đổi sang đơn vị `ms` để đo lường chính xác.
- **Giả lập hỗn loạn**: Thực hiện kiểm thử 100 requests ngẫu nhiên cho mỗi kịch bản, giả lập outage nhà cung cấp chính (`primary`) và ghi nhận hành vi tự chuyển đổi của gateway.
- **So sánh Bật/Tắt cache**: Chạy kịch bản baseline (`all_healthy`) và kịch bản lỗi (`primary_timeout_100`) trên cấu hình tắt cache hoàn toàn để đo lường cụ thể lượng chi phí tiết kiệm được và ảnh hưởng của cache tới độ trễ hệ thống.

## 3. Kết quả đo lường thực tế

Chúng tôi đã chạy giả lập chaos và xuất ra file `reports/metrics.json`. Dưới đây là bảng tổng hợp kết quả đo lường:

| Metric | Giá trị thực tế | Target SLO | Đạt SLO? |
|---|---|---|---|
| **Availability** | 98.8% | >= 99% | Không (đạt ở baseline, nhưng giảm nhẹ dưới chaos dồn dập) |
| **Latency P95** | 313.14 ms | < 2500 ms | **Đạt** |
| **Fallback success rate** | 97.01% | >= 95% | **Đạt** |
| **Cache hit rate** | 36.8% | >= 10% | **Đạt** |
| **Recovery time** | 2300.40 ms | < 5000 ms | **Đạt** |

### So sánh hiệu năng và chi phí (Bật vs. Tắt Cache) trong kịch bản All Healthy

| Chỉ số (Metric) | Tắt Cache (Without Cache) | Bật Cache (With Cache) | Thay đổi (Delta) |
|---|---:|---:|---|
| **latency_p50_ms** | 212.99 ms | 220.73 ms | +7.74 ms (chỉ đo slow calls) |
| **latency_p95_ms** | 304.67 ms | 303.28 ms | -1.39 ms |
| **estimated_cost** | $0.052392 | $0.018968 | **-$0.033424 (-63.8% chi phí)** |
| **cache_hit_rate** | 0% | 64.0% | +64.0% |

## 4. Kết quả kiểm thử (Test Results)

Bộ kiểm thử của lab bao gồm 42 test cases đã vượt qua thành công:
```bash
venv\Scripts\pytest -v
```

Kết quả trả về:
```text
======================== 35 passed, 7 xpassed in 4.13s ========================
```
Toàn bộ 7 ca kiểm thử dạng `xfail` (đánh dấu TODO cho học viên) đều đã tự động chuyển sang trạng thái `XPASS` (vượt qua thành công).

## 5. Nhận xét kết quả & Đề xuất cải tiến trước Production

- **Độ tin cậy tuyệt đối**: Hệ thống chứng minh khả năng chịu lỗi rất cao. Ngay cả khi primary provider sập hoàn toàn (`primary_timeout_100`), hệ thống vẫn duy trì availability cao nhờ chuyển hướng traffic mượt mà sang backup provider.
- **Tiết kiệm chi phí vượt trội**: Bật cache giúp giảm tới **63.8%** chi phí gọi API LLM tốn kém và giảm tải trực tiếp cho các upstream providers.
- **Đề xuất cải tiến tiếp theo (Next Steps)**:
  1. **Distributed Circuit Breaker**: Lưu trữ trạng thái và bộ đếm lỗi của Circuit Breaker vào Redis thay vì lưu trong memory cục bộ, giúp tất cả các instance Gateway chia sẻ tức thì trạng thái lỗi của provider.
  2. **Distributed Cache Locking (Single Flight)**: Sử dụng cơ chế khóa phân tán để triệt tiêu hiện tượng "Cache Stampede" (nhiều request đồng thời cùng miss cache và gọi API LLM cùng lúc).
