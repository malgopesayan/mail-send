"""In-memory stand-ins for Firestore, Supabase Storage and Gmail."""
import copy
import itertools

_ids = itertools.count(1)


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


class Snap:
    def __init__(self, ref):
        self.reference, self.id = ref, ref.id
        self.exists = ref.path in ref.store
        self._data = copy.deepcopy(ref.store.get(ref.path))

    def to_dict(self):
        return copy.deepcopy(self._data)


class Doc:
    def __init__(self, store, path):
        self.store, self.path, self.id = store, path, path[-1]

    def get(self):
        return Snap(self)

    def set(self, data, merge=False):
        self.store[self.path] = _merge(self.store.get(self.path, {}), data) if merge else copy.deepcopy(data)

    def update(self, data):
        if self.path not in self.store:
            raise KeyError("no document")
        self.store[self.path].update(copy.deepcopy(data))

    def delete(self):
        self.store.pop(self.path, None)

    def collection(self, name):
        return Col(self.store, self.path + (name,))


class Col:
    def __init__(self, store, path, filters=(), order=None, cap=None):
        self.store, self.path, self.filters, self.order, self.cap = store, path, filters, order, cap

    def document(self, doc_id=None):
        return Doc(self.store, self.path + (doc_id or f"id{next(_ids)}",))

    def add(self, data):
        ref = self.document()
        ref.set(data)
        return None, ref

    def where(self, field, op, value):
        assert op == "=="
        return Col(self.store, self.path, self.filters + ((field, value),), self.order, self.cap)

    def order_by(self, field, direction="ASCENDING"):
        return Col(self.store, self.path, self.filters, (field, direction == "DESCENDING"), self.cap)

    def limit(self, n):
        return Col(self.store, self.path, self.filters, self.order, n)

    def stream(self):
        n = len(self.path) + 1
        rows = [Snap(Doc(self.store, p)) for p in list(self.store) if len(p) == n and p[:-1] == self.path]
        rows = [r for r in rows if all(r.to_dict().get(f) == v for f, v in self.filters)]
        if self.order:
            rows.sort(key=lambda r: r.to_dict().get(self.order[0]) or "", reverse=self.order[1])
        return rows[: self.cap] if self.cap else rows

    def count(self):
        total = len(self.stream())

        class Count:
            def get(_):
                return [[type("R", (), {"value": total})()]]
        return Count()


class Batch:
    def __init__(self):
        self.ops = []

    def set(self, ref, data):
        self.ops.append(lambda: ref.set(data))

    def delete(self, ref):
        self.ops.append(ref.delete)

    def commit(self):
        for op in self.ops:
            op()


class FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return Col(self.store, (name,))

    def batch(self):
        return Batch()


class FakeStorage:
    def __init__(self):
        self.files = {}

    def upload(self, bucket, path, data, content_type=None):
        self.files[(bucket, path)] = data

    def download(self, bucket, path):
        return self.files[(bucket, path)]

    def list_files(self, bucket, prefix, limit=200):
        pre = prefix.rstrip("/") + "/"
        return [{"id": p, "name": p[len(pre):], "metadata": {"size": len(d)}}
                for (b, p), d in self.files.items() if b == bucket and p.startswith(pre)]

    def remove(self, bucket, paths):
        for p in paths:
            self.files.pop((bucket, p), None)

    def signed_url(self, *a, **k):
        return None

    def ping(self):
        return True


class FakeSender:
    outbox: list = []

    def __init__(self, secrets=None, from_name="", from_email="me@example.com"):
        self.from_email = from_email or "me@example.com"

    def send(self, to, subject, body, attachment=None, filename=None, thread_id=None, reply_to_id=None):
        n = len(self.outbox) + 1
        self.outbox.append({"to": to, "subject": subject, "body": body, "filename": filename,
                            "attachment": attachment, "thread_id": thread_id, "reply_to_id": reply_to_id})
        return {"id": f"m{n}", "thread_id": thread_id or f"t{n}", "message_id": f"<m{n}@example.com>"}
