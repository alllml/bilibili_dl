"""
公共下载工具函数

【职责】HTTP 下载 + 进度回报 + 可取消 + 多线程分片加速 + 并发自适应。
所有 Extractor 的 download() 方法共用此函数完成实际的文件下载，
避免每个插件重复实现 HTTP 下载逻辑。

【为什么需要多线程分片】
B站等 CDN 对**单条连接**有明显限速，且长连接容易被掐断
（实测反复出现 SSLEOFError / IncompleteRead）。
拆成 N 个 Range 分片并行下载，既绕过单连接限速，也避免一条连接断掉就全盘重来。

【为什么要"自适应"而不是固定线程数】
固定线程数是拍脑袋的：网差时开 16 条只会互相抢带宽、更容易被风控；
网好时开 4 条又吃不满。于是这里改成：
  1. 把文件切成**小片放进共享队列**，哪个线程空了就去取下一片
     —— 快慢线程不会互相拖累，不会出现"3 条下完干等 1 条"；
  2. 并发数**从小往大爬**，每爬一级测一次整体吞吐：
     只有确实更快才保留更高并发，没提升就**退回上一档**（说明到瓶颈了）；
  3. 单片失败**不放弃整体**：失败片记下来，其它片继续下，
     最后再低并发补下失败的分片（用户网络抖动时不会白下几十 MB）；
  4. 全部完成后**校验字节数**，防止静默截断产出坏文件；
  5. 多个 CDN 候选先**限时测速择优选址**（见 probe_fastest_url），
     避免"不报错但很慢"的 CDN 一直拖后腿。

【重要限制】
分片加速只在「CDN 对单连接限速、但链路总带宽有富余」时有效。
如果瓶颈在链路本身（例如走 VPN 隧道，总带宽只有几百 KB/s），
那么开更多连接**不会更快**——此时自适应会检测到"没有提升"并自动退回低并发，
日志里会打印 `并发 N 未带来提升，回退到 M 条连接`。
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class DownloadCancelled(Exception):
    """用户主动取消下载时抛出（用于与网络错误区分开）"""


class _Aborted(Exception):
    """内部使用：其它分片已失败/用户已取消，当前分片提前退出（不记录为错误）"""


class _RangeUnsupported(Exception):
    """内部使用：服务端对 start>0 仍返回 200（忽略 Range），重试无意义"""


# 小于该体积的文件不值得分片（探测开销 + 并发收益都很小）
_MIN_MULTI_SIZE = 2 * 1024 * 1024

_CHUNK_SIZE = 65536  # 64KB，单次网络读取粒度

# 分片粒度：太小会请求过多，太大会让并发分配不均衡
_MIN_CHUNK = 1 * 1024 * 1024        # 1MB
_MAX_CHUNK = 8 * 1024 * 1024        # 8MB
_CHUNKS_PER_WORKER = 6              # 每个线程至少分到这么多片，保证负载均衡

# 自适应并发的探测窗口与判定阈值
_CONTROL_WINDOW = 2.0               # 秒：每级并发的观察窗口
_RAMP_GAIN = 1.10                   # 吞吐提升 ≥10% 才认为"加并发真的有用"
_DROP_RATIO = 0.70                  # 吞吐掉到峰值 70% 以下视为链路变差
_MAX_EXPLORE = 4                    # 最多向上试探几档

# 分片失败后的补下轮次（每轮并发更小）
_CHUNK_RETRY_PASSES = 3

# 连接 / 读取超时（读取超时按"单次 socket 读"计，不限制总时长）
_CONNECT_TIMEOUT = 15
_READ_TIMEOUT = 90

# CDN 测速：**限时**采样，避免在慢网络上把时间耗在测速上
_SPEED_SAMPLE_BYTES = 256 * 1024    # 采样上限（快网络上到此即止）
_SPEED_SAMPLE_SECONDS = 0.8         # 单个 CDN 最长测速时间
_SPEED_MAX_PROBE = 3                # 最多实测几个候选（其余按原顺序兜底）


def _make_session() -> requests.Session:
    """创建带自动重试的 Session，应对 CDN 连接不稳定。"""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retries, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _remove_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _pick_chunk_size(total: int, workers: int) -> int:
    """按文件大小与预期并发数挑选分片粒度，让每个线程都有足够多的片可取。"""
    if workers < 1:
        workers = 1
    ideal = total // (workers * _CHUNKS_PER_WORKER)
    chunk = max(_MIN_CHUNK, min(ideal, _MAX_CHUNK))
    return max(chunk, 1)


def _probe_total_size(url: str, headers: dict) -> int:
    """
    探测文件总大小，同时判断服务端是否支持 Range 分片。
    返回 >0 表示「支持分片且拿到总大小」；0 表示不支持 / 无法判断。
    """
    probe_headers = dict(headers or {})
    probe_headers["Range"] = "bytes=0-0"
    resp = None
    try:
        resp = requests.get(url, headers=probe_headers, stream=True,
                            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
        if resp.status_code == 206:
            content_range = resp.headers.get("Content-Range", "")
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[1].strip()
                if total.isdigit():
                    return int(total)
            return 0
        # 200 说明服务端忽略了 Range（会返回整个文件）→ 不支持分片
        return 0
    except Exception:
        return 0
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def _measure_speed(url: str, headers: dict, sample_bytes: int,
                   max_seconds: float) -> float:
    """
    **限时**测一下这个地址的下载速度（字节/秒）。

    与固定采样量不同：不管有没有下够 sample_bytes，最多只观察 max_seconds。
    这样在慢网络上（例如 80KB/s）也不会为了测速白等十几秒。

    **计时从收到首字节开始**：DNS/TCP/TLS 握手与首包延迟是可变的，
    如果算进分母，采样窗口越短误差越大 —— 实测在走 VPN 的链路上，
    把一个真实 2.2MB/s 的地址误测成 0.05MB/s（差了 40 倍），
    结果就是"测速结果全是 0.05"，择优排序失去意义。

    返回 0.0 表示完全测不出来（连不上 / 出错），调用方按"未知"处理。
    """
    resp = None
    try:
        probe_headers = dict(headers or {})
        probe_headers["Range"] = f"bytes=0-{sample_bytes - 1}"
        request_start = time.time()
        deadline = request_start + max_seconds
        resp = requests.get(url, headers=probe_headers, stream=True,
                            timeout=(_CONNECT_TIMEOUT, max(_READ_TIMEOUT, 15)))
        if resp.status_code not in (200, 206):
            return 0.0
        got = 0
        first_byte_at = None
        last_byte_at = None
        for data in resp.iter_content(chunk_size=_CHUNK_SIZE):
            if data:
                if first_byte_at is None:
                    first_byte_at = time.time()
                got += len(data)
                last_byte_at = time.time()
            if got >= sample_bytes or time.time() >= deadline:
                break
        if got <= 0 or first_byte_at is None:
            return 0.0
        # 传输耗时（不含握手）；采样过短（极快链路）时退化为整个请求耗时，
        # 避免除以接近 0 的值而虚高
        transfer = last_byte_at - first_byte_at
        elapsed = transfer if transfer >= 0.1 else (last_byte_at - request_start)
        return got / max(elapsed, 1e-3)
    except Exception:
        return 0.0
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def probe_fastest_url(candidates, headers: dict, cancel_event=None,
                      sample_bytes: int = _SPEED_SAMPLE_BYTES,
                      max_seconds: float = _SPEED_SAMPLE_SECONDS,
                      max_probe: int = _SPEED_MAX_PROBE,
                      verbose: bool = True) -> list:
    """
    对多个 CDN 候选地址做**限时**测速，返回按实测速度降序排列的新列表。

    目的：解决"CDN 不报错但很慢"的问题——之前只按接口返回顺序取第一个，
    一旦命中限速的 CDN（例如 Akamai），下载就一直是龟速且永远不切换。

    成本上限 = max_probe × max_seconds（默认 3 × 0.8s ≈ 2.4 秒）。
    测不出速度的地址排在有速度的之后并保持原有相对顺序，
    所以即使测速失败，行为也和原来一致（不会更差）。
    """
    candidates = list(candidates or [])
    if len(candidates) < 2:
        return candidates

    measured = []    # (speed, 原序号, url)
    unknown = []     # (原序号, url)

    for index, url in enumerate(candidates):
        if cancel_event is not None and cancel_event.is_set():
            unknown.extend((i, u) for i, u in enumerate(candidates[index:], index))
            break
        if index >= max_probe:
            unknown.append((index, url))
            continue
        speed = _measure_speed(url, headers, sample_bytes, max_seconds)
        if speed > 0:
            measured.append((speed, index, url))
        else:
            unknown.append((index, url))

    measured.sort(key=lambda item: (-item[0], item[1]))

    if verbose and measured:
        detail = "，".join(
            f"{url.split('/')[2] if '//' in url else url} {speed / 1024 / 1024:.2f}MB/s"
            for speed, _, url in measured[:4])
        print(f"  CDN 测速：{detail}")

    return [url for _, _, url in measured] + [url for _, url in unknown]


class _ChunkQueue:
    """线程安全的分片队列：谁空了谁来取下一片（动态分配，负载自动均衡）。"""

    def __init__(self, total: int, chunk_size: int):
        self._chunks = []
        start = 0
        while start < total:
            end = min(start + chunk_size - 1, total - 1)
            self._chunks.append((start, end))
            start = end + 1
        self._index = 0
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            if self._index >= len(self._chunks):
                return None
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    def exhausted(self) -> bool:
        with self._lock:
            return self._index >= len(self._chunks)

    def all_chunks(self) -> list:
        """全部分片列表（用于事后核对哪些还没下完）"""
        return list(self._chunks)


def _fetch_chunk(session, url, headers, save_path, chunk,
                 cancel_event, stop_event, attempts: int = 3,
                 on_bytes=None) -> int:
    """
    下载单个分片并写入文件对应偏移；失败自动重试（同一区间重新拉取）。
    返回实际写入字节数。

    on_bytes: 每读到一段数据就回调 on_bytes(n)，**边下边报**。
      这一点很关键：如果只在整片下完时才计数，慢速链路（例如 80KB/s）下
      1MB 的分片要十几秒才 +1 次，自适应爬坡会误判成"没有速度"而放弃加并发，
      进度条也会长时间不动。

    抛 _RangeUnsupported 表示服务端根本不支持 Range —— 重试无意义，交给上层放弃分片。
    """
    start, end = chunk
    expected = end - start + 1
    last_error = None
    covered = 0      # 本分片已"覆盖"的字节数（重试会覆盖重写，故跨尝试取最大值）

    for attempt in range(1, attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("用户已取消下载")
        if stop_event.is_set():
            raise _Aborted()

        resp = None
        received = 0
        attempt_bytes = 0     # 本次尝试从区间开头起已读到的字节
        try:
            part_headers = dict(headers or {})
            part_headers["Range"] = f"bytes={start}-{end}"
            resp = session.get(url, headers=part_headers, stream=True,
                               timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
            if resp.status_code == 200 and start > 0:
                raise _RangeUnsupported("服务端未按 Range 返回分片")
            resp.raise_for_status()

            with open(save_path, "r+b") as f:
                f.seek(start)
                for data in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("用户已取消下载")
                    if stop_event.is_set():
                        raise _Aborted()
                    if data:
                        f.write(data)
                        received += len(data)
                        attempt_bytes += len(data)
                        # 只上报"新覆盖"的部分：重试是把同一区间从头重写，
                        # 若每次都全额上报，进度会虚高；若失败时回滚，
                        # 又会把重写覆盖掉的字节重复扣掉（曾因此少算 832KB）
                        if on_bytes is not None and attempt_bytes > covered:
                            on_bytes(attempt_bytes - covered)
                            covered = attempt_bytes

            if received != expected:
                raise RuntimeError(f"分片长度不符（{received}/{expected}）")
            return received

        except (DownloadCancelled, _Aborted, _RangeUnsupported):
            raise
        except Exception as e:
            last_error = e
            if attempt < attempts:
                # 简单退避后重试同一分片；连接已被掐断时重连往往就恢复了
                time.sleep(0.4 * attempt)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    raise last_error if last_error else RuntimeError("分片下载失败")


class _ConcurrencyController:
    """
    全程持续调节并发数（不是只在下载开头试几秒）。

    为什么需要"持续"：
      实测发现（用户走 VPN 下 B站 786MB 视频）：
        · 16 条并发 → 1.70 MB/s
        · 1 条并发  → 2.19 MB/s
      因为瓶颈在隧道**总带宽**，连接越多开销越大。而旧实现只在开头试探一次，
      一旦被 TCP 慢启动 / 抖动骗到就会一路加到上限，然后全程不回头。

    算法：
      1. **预热窗口**：每次改变并发后先空一个窗口不计入判断
         —— 新连接刚建立时吞吐天然偏低，不隔开就会把"握手变快"误判成"加并发有效"；
      2. **逐档试探**：从起始并发开始翻倍试探，只有实测吞吐真的提升（≥10%）才保留；
      3. **无提升即回退**：某档没有提升就退回上一档，并停止向上探索；
      4. **持续监测**：之后一直盯吞吐，掉到峰值 70% 以下连续两个窗口就主动降并发。
    """

    def __init__(self, state, lock, queue, stop_event, spawn, ceiling,
                 start_count, notes):
        self.state = state
        self.lock = lock
        self.queue = queue
        self.stop_event = stop_event
        self.spawn = spawn
        self.ceiling = ceiling
        self.start_count = start_count
        self.notes = notes
        self.settled_conc = start_count
        self.settled_speed = 0.0
        self.probe_from = None      # 试探前的并发（None 表示当前没有试探在飞行中）
        self.probe_speed = 0.0
        self.warmup = 0
        self.explore_left = _MAX_EXPLORE
        self.low_streak = 0

    def _set_target(self, value):
        with self.lock:
            self.state["target"] = value

    def run(self):
        last_bytes = 0
        # 注意：这里不因为"分片已全部分配完"就退出 —— 分片分配完 ≠ 数据下完，
        # 此时仍在传输，仍然需要判断并发是否合适（否则刚加的并发就没人管了）。
        while not self.stop_event.is_set():
            time.sleep(_CONTROL_WINDOW)
            if self.stop_event.is_set():
                return
            with self.lock:
                current = self.state["done"]
                conc = self.state["target"]
            speed = (current - last_bytes) / _CONTROL_WINDOW
            last_bytes = current
            if speed <= 0:
                continue
            if self.warmup > 0:
                # 刚改变过并发：这个窗口只是让新连接热起来，不参与判断
                self.warmup -= 1
                continue

            if self.probe_from is not None:
                self._judge_probe(conc, speed)
                continue

            if self.settled_speed <= 0:
                # 建立基线
                self.settled_speed = speed
                self.settled_conc = conc
            if self.explore_left > 0 and conc < self.ceiling:
                self._start_probe(conc, speed)
            else:
                self._monitor(speed)

    def _start_probe(self, conc, speed):
        self.explore_left -= 1
        nxt = min(self.ceiling, max(conc + 1, conc * 2))
        if nxt <= conc:
            self.explore_left = 0
            return
        self.probe_from = conc
        self.probe_speed = speed
        self._set_target(nxt)
        self.spawn(nxt - conc)
        self.warmup = 1
        self.notes.append(
            f"并发 {conc} 档吞吐 {speed / 1048576:.2f} MB/s，试着加到 {nxt} 条看看")

    def _judge_probe(self, conc, speed):
        origin = self.probe_from
        if speed >= self.probe_speed * _RAMP_GAIN:
            # 加并发确实更快，保留
            self.settled_conc = conc
            self.settled_speed = speed
            self.notes.append(
                f"并发 {conc} 有效（{speed / 1048576:.2f} MB/s），保留")
        else:
            # 没有提升 → 退回，并停止继续加
            self._set_target(origin)
            self.settled_conc = origin
            self.settled_speed = max(self.probe_speed, speed)
            self.explore_left = 0
            self.notes.append(
                f"并发 {conc} 未带来提升（{speed / 1048576:.2f} MB/s），"
                f"回退到 {origin} 条连接（链路总带宽可能是瓶颈）")
        self.probe_from = None
        self.warmup = 1

    def _monitor(self, speed):
        if self.settled_speed > 0 and speed < self.settled_speed * _DROP_RATIO:
            self.low_streak += 1
            if self.low_streak >= 2:
                new_conc = max(1, self.settled_conc // 2)
                if new_conc < self.settled_conc:
                    self._set_target(new_conc)
                    self.notes.append(
                        f"吞吐持续偏低（{speed / 1048576:.2f} MB/s），"
                        f"并发降到 {new_conc} 条")
                self.settled_conc = new_conc
                self.settled_speed = speed
                self.low_streak = 0
        else:
            self.low_streak = 0
            if speed > self.settled_speed:
                self.settled_speed = speed


def _download_multi(url: str, headers: dict, save_path: str, total: int,
                    connections: int, progress_callback=None,
                    cancel_event=None, adaptive: bool = True) -> str:
    """
    多连接分片下载（HTTP Range）。

    connections 是**并发上限**：
      - adaptive=True：从较小并发起步，按实测吞吐自动升降，到瓶颈即停；
      - adaptive=False：直接按 connections 固定并发。

    实现要点：
      * 分片放在共享队列里动态分配 —— 不会出现"3 条下完干等 1 条"；
      * 并发用 alive/target 计数控制，降并发时多余线程会自己退出；
      * 单片失败**不中断整体**：记录下来继续下其它片，最后低并发补下；
      * **完整性以"分片集合"为准，而不是累计字节数** —— 字节累加一旦因为重试
        出错（少算/多算）就无法知道缺的是哪一片，也就无法补下；
      * 服务端不支持 Range 或用户取消时立即整体中止。
    """
    connections = max(1, int(connections))
    chunk_size = _pick_chunk_size(total, connections)
    queue = _ChunkQueue(total, chunk_size)
    all_chunks = queue.all_chunks()

    lock = threading.Lock()
    stop_event = threading.Event()
    cancel_seen: list = []      # 取消信号
    fatal: list = []            # 致命错误（不支持 Range）
    done_chunks: set = set()    # 已完整下完的分片（完整性的唯一判据）
    notes: list = []            # 需要打印给用户的提示（降并发等）
    # target = 期望并发；alive = 当前实际存活线程数；done = 已下字节（仅用于进度显示）
    state = {"done": 0, "last_report": 0.0, "alive": 0, "target": 1}

    started_at = time.time()

    # 预分配文件，各线程按偏移写入
    with open(save_path, "wb") as f:
        f.truncate(total)

    def bump(received: int):
        """累计已下字节并按节流回报进度（仅用于进度显示，不参与完整性判断）"""
        with lock:
            state["done"] = min(total, max(0, state["done"] + received))
            now = time.time()
            report = None
            if progress_callback and now - state["last_report"] >= 0.2:
                state["last_report"] = now
                report = (state["done"], total)
        if report and progress_callback:
            progress_callback(*report)

    def worker():
        with lock:
            state["alive"] += 1
        released = False
        session = _make_session()
        try:
            while not stop_event.is_set():
                # 并发被下调时，多余线程在取下一片之前主动退出
                with lock:
                    if state["alive"] > state["target"]:
                        state["alive"] -= 1
                        released = True
                        return
                chunk = queue.next()
                if chunk is None:
                    return
                try:
                    _fetch_chunk(session, url, headers, save_path,
                                 chunk, cancel_event, stop_event,
                                 on_bytes=bump)
                except _Aborted:
                    return
                except DownloadCancelled as e:
                    with lock:
                        cancel_seen.append(e)
                    stop_event.set()
                    return
                except _RangeUnsupported as e:
                    with lock:
                        fatal.append(e)
                    stop_event.set()
                    return
                except Exception as e:
                    # 关键：单片失败不放弃整体（该片不在 done_chunks 里，
                    # 后面统一按"缺失分片"补下），并主动收敛并发
                    with lock:
                        if state["target"] > 1:
                            new_target = max(1, state["target"] // 2)
                            state["target"] = new_target
                            note = (f"检测到连接中断（{type(e).__name__}），"
                                    f"并发自动降到 {new_target} 条（链路不稳）")
                            if not notes or notes[-1] != note:
                                notes.append(note)
                    continue
                else:
                    with lock:
                        done_chunks.add(chunk)
        finally:
            try:
                session.close()
            except Exception:
                pass
            if not released:
                with lock:
                    state["alive"] -= 1

    threads: list = []
    threads_lock = threading.Lock()

    def spawn(count: int):
        for _ in range(count):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            with threads_lock:
                threads.append(t)

    # 起始并发：自适应时**从低处起步**（2 条），然后靠实测吞吐往上加。
    # 从低起步的理由：多连接被掐断 / 相互抢带宽这类问题，只有"少开"才能避免；
    # 而少开造成的损失很小 —— 有提升的话控制器几秒内就会加上去。
    # 固定模式则直接开满。
    if adaptive:
        start_count = min(connections, 2)
    else:
        start_count = connections
    with lock:
        state["target"] = start_count
    spawn(start_count)

    # ---- 全程持续调节并发（见 _ConcurrencyController 的说明）----
    controller = None
    can_adapt = adaptive and connections > start_count
    if can_adapt:
        controller = _ConcurrencyController(
            state, lock, queue, stop_event, spawn, connections,
            start_count, notes)
        ctrl_thread = threading.Thread(target=controller.run, daemon=True)
        ctrl_thread.start()
    else:
        ctrl_thread = None

    with threads_lock:
        snapshot = list(threads)
    for t in snapshot:
        t.join()

    # 分片都下完了，让控制器收工
    stop_event.set()
    if ctrl_thread is not None:
        ctrl_thread.join(timeout=_CONTROL_WINDOW + 1)

    with lock:
        pending_notes = list(notes)
    for note in pending_notes:
        print(f"  {note}")

    # 取消 / 致命错误优先返回
    if cancel_seen or (cancel_event is not None and cancel_event.is_set()):
        raise DownloadCancelled("用户已取消下载")
    if fatal:
        raise fatal[0]

    # ---- 缺失分片低并发补下（保留已完成进度，不整体重来）----
    #
    # 判据是"哪些分片还没进 done_chunks"，而不是"累计字节够不够"。
    # 字节累加在重试/回滚/中途退出等情况下都可能失真；一旦失真，
    # 就不知道缺的是哪一片，也就无从补起（曾出现过"少 832KB 却 0 片失败"）。
    for attempt in range(1, _CHUNK_RETRY_PASSES + 1):
        with lock:
            missing = [c for c in all_chunks if c not in done_chunks]
        if not missing:
            break
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("用户已取消下载")
        print(f"  有 {len(missing)} 个分片未下完，第 {attempt} 轮低并发补下...")
        time.sleep(0.5 * attempt)
        stop_event.clear()

        def retry_one(chunk):
            session = _make_session()
            try:
                _fetch_chunk(session, url, headers, save_path,
                             chunk, cancel_event, stop_event, on_bytes=bump)
                return chunk, None
            except (DownloadCancelled, _Aborted) as e:
                return chunk, e
            except Exception as e:
                return chunk, e
            finally:
                try:
                    session.close()
                except Exception:
                    pass

        workers = max(1, min(4, connections))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(retry_one, c) for c in missing]
            for fut in as_completed(futures):
                chunk, err = fut.result()
                if err is None:
                    with lock:
                        done_chunks.add(chunk)
                elif isinstance(err, DownloadCancelled):
                    raise err
                # 失败的分片本来就不在 done_chunks 里，下一轮自动再试

    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("用户已取消下载")

    with lock:
        got = state["done"]
        missing_count = len(all_chunks) - len(done_chunks)

    if missing_count:
        with lock:
            still = [c for c in all_chunks if c not in done_chunks]
        head = "、".join(f"{s}-{e}" for s, e in still[:3])
        raise RuntimeError(
            f"分片下载不完整（{missing_count}/{len(all_chunks)} 个分片始终下不下来，"
            f"例如 {head}；已下 {got}/{total} 字节）")

    cost = max(time.time() - started_at, 1e-3)
    avg = total / cost / 1024 / 1024
    with lock:
        final_target = state["target"]
    print(f"  分片下载完成（并发 {final_target} 条，平均 {avg:.2f} MB/s）")

    if progress_callback:
        progress_callback(total, total)
    return save_path


def download_file(
    url: str,
    headers: dict,
    save_path: str,
    progress_callback=None,
    cancel_event=None,
    connections: int = 1,
    adaptive: bool = True,
) -> str:
    """
    下载文件到 save_path（支持取消；connections>1 时多线程分片加速）。

    参数：
        progress_callback: callback(downloaded_bytes, total_bytes)，total 可能为 0
        cancel_event:      threading.Event，置位后立即中断并抛 DownloadCancelled
        connections:       并发上限（1 表示单线程）
        adaptive:          并发是否自适应（默认 True；False 则固定为 connections）

    返回：保存路径；取消时抛 DownloadCancelled 并清理未完成的文件。
    """
    # ---- 优先尝试多线程分片 ----
    if connections and connections > 1:
        total = _probe_total_size(url, headers)
        if total >= _MIN_MULTI_SIZE:
            try:
                return _download_multi(url, headers, save_path, total,
                                       int(connections), progress_callback,
                                       cancel_event, adaptive=bool(adaptive))
            except DownloadCancelled:
                _remove_quietly(save_path)
                raise
            except Exception as e:
                print(f"  多线程下载失败（{type(e).__name__}: {e}），回退单线程重试")
                _remove_quietly(save_path)

    # ---- 单线程下载（同时作为多线程失败后的回退）----
    session = _make_session()
    resp = None
    try:
        resp = session.get(url, headers=headers, stream=True,
                           timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
        resp.raise_for_status()

        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0

        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("用户已取消下载")
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        return save_path
    except DownloadCancelled:
        _remove_quietly(save_path)
        raise
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
