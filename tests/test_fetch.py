from dhbw_scraper import fetch as f


class FakeResp:
    def __init__(self, data=b"", status=200, headers=None, url="http://x/"):
        self._data = data
        self.status = status
        self.headers = FakeHeaders(headers or {})
        self._url = url

    def read(self):
        return self._data

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHeaders:
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get_content_type(self):
        return self._d.get("content-type", "text/html").split(";")[0]

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)


def test_classify_routing():
    assert f.classify("text/html", "http://x/a") == "html"
    assert f.classify("application/pdf", "http://x/a") == "pdf"
    assert f.classify("", "http://x/a.pdf") == "pdf"
    assert f.classify("image/png", "http://x/a.png") == "other"
    assert f.classify("", "http://x/startseite") == "html"


def test_fetch_success_captures_validators():
    def opener(req, timeout=0):
        assert req.get_header("User-agent") == "ua"
        return FakeResp(
            b"<html>hi</html>",
            200,
            {
                "Content-Type": "text/html",
                "ETag": 'W/"abc"',
                "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            },
        )

    r = f.fetch("http://x/a", "ua", opener=opener)
    assert r.ok and r.data == b"<html>hi</html>"
    assert r.etag == 'W/"abc"'
    assert r.last_modified == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_fetch_conditional_sends_validators():
    seen = {}

    def opener(req, timeout=0):
        seen["inm"] = req.get_header("If-none-match")
        seen["ims"] = req.get_header("If-modified-since")
        return FakeResp(b"x")

    f.fetch("http://x/a", "ua", etag='"abc"', last_modified="LM", opener=opener)
    assert seen["inm"] == '"abc"'
    assert seen["ims"] == "LM"


def test_fetch_304_is_not_modified():
    import urllib.error

    def opener(req, timeout=0):
        raise urllib.error.HTTPError("http://x/a", 304, "Not Modified", {}, None)

    r = f.fetch("http://x/a", "ua", etag='"abc"', opener=opener)
    assert r.not_modified
    assert not r.ok
    assert r.status == 304


def test_fetch_404_returns_error_result():
    import urllib.error

    def opener(req, timeout=0):
        raise urllib.error.HTTPError("http://x/a", 404, "Not Found", {}, None)

    r = f.fetch("http://x/a", "ua", opener=opener)
    assert r.status == 404 and not r.ok and not r.not_modified
