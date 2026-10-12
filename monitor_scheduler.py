"""Persistent recurring jobs; deliberately independent of any browser session."""
import signal
import time

from redis.exceptions import RedisError

from detection_store import DetectionStore
from monitor_store import DUE, MonitorStore

stopping = False


def shutdown(signum, frame):
    global stopping
    stopping = True


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    backend = MonitorStore(DetectionStore())
    while not stopping:
        try:
            backend.redis.set("modeltrace:scheduler:heartbeat", time.time(), ex=15)
            backend.migrate_visibility()
            backend.tasks.reap()
            for monitor_id in backend.redis.zrangebyscore(DUE, "-inf", time.time(), start=0, num=100):
                if stopping:
                    break
                backend.tick(monitor_id)
        except RedisError:
            pass
        time.sleep(2)


if __name__ == "__main__":
    main()
