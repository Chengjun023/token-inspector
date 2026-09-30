from collections import OrderedDict


class Cache:
    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be an integer greater than zero")
        self.capacity = capacity
        self.data = OrderedDict()

    def _remove_expired(self, now):
        expired = [key for key, (_, expiry) in self.data.items() if now >= expiry]
        for key in expired:
            del self.data[key]

    def put(self, key, value, now, ttl):
        self._remove_expired(now)
        if ttl == 0:
            self.data.pop(key, None)
            return
        if key not in self.data and len(self.data) >= self.capacity:
            self.data.popitem(last=False)
        self.data[key] = (value, now + ttl)
        self.data.move_to_end(key)

    def get(self, key, now):
        self._remove_expired(now)
        value, expiry = self.data[key]
        self.data.move_to_end(key)
        return value
