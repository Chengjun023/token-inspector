class Cache:
    def __init__(self, capacity):
        self.capacity = capacity
        self.data = {}

    def put(self, key, value, now, ttl):
        if key not in self.data and len(self.data) >= self.capacity:
            self.data.pop(next(iter(self.data)))
        self.data[key] = (value, now + ttl)

    def get(self, key, now):
        value, expiry = self.data[key]
        if now > expiry:
            del self.data[key]
            raise KeyError(key)
        return value
