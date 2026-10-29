from queue import Queue

from . import config, ingest as ingest_mod, tasks
from .classify import classify_one


def build_lines(names, focus=None, targets=None, force_kind=None):
    """取名线与摄入线并行：取名线每入库一份就把路径塞进队列，摄入线同时取走摄入，两条线互不等待。

    网页上传（api._pipeline）与取数入库（fetcher）共用这一套双线。force_kind 给已知
    类型的取数源用（如年报强制归为 filing），保证后续走对应摄入流程。
    """
    queue = Queue()
    focus = focus or []
    targets = targets or []

    def classify_line(tid):
        results = []
        try:
            for i, name in enumerate(names, 1):
                tasks.update(tid, current=i - 1, detail=f"第 {i}/{len(names)} 份：{name} 正在取名并进入资料库")
                p = config.INBOX_DIR / name
                if not p.exists():
                    results.append({"ok": False, "file": name, "error": "文件不在收件台"})
                    continue
                try:
                    r = classify_one(p, force_kind=force_kind)
                except Exception as e:
                    r = {"ok": False, "file": name, "error": str(e)}
                results.append(r)
                if r.get("ok"):
                    queue.put(config.ROOT / r["target"])
                tasks.update(tid, current=i)
        finally:
            queue.put(None)  # 收工信号：摄入线做完队列里剩下的就退出
        tasks.update(tid, current=len(names), detail="")
        return results

    def ingest_line(tid):
        results = []
        done = 0
        while True:
            target = queue.get()
            if target is None:
                break
            done += 1
            tasks.update(tid, current=done - 1, detail=f"第 {done} 份：{target.name} 正在摄入知识点")
            manifest = ingest_mod.load_manifest()
            before = {f["sha256"] for f in manifest["files"]}
            try:
                r = ingest_mod.ingest_one(target, manifest, focus=focus, targets=targets)
            except Exception as e:
                print(f"  摄入失败: {e}")
                r = {"file": target.name, "error": str(e)}
            if r:
                results.append(r)
            # 逐份落盘，且只追加本轮新增的条目：删除线并发删掉的条目不会被这份旧清单冲回来
            ingest_mod.append_manifest([f for f in manifest["files"] if f["sha256"] not in before])
            tasks.update(tid, current=done)
        tasks.update(tid, current=len(names), detail="")
        ingest_mod.finish(ingest_mod.load_manifest(), [r for r in results if "error" not in r])
        return results

    return classify_line, ingest_line
