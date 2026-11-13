import re

import pytest

from attsite.monitor import (
    Diff, ExtractError, compute_diff, extract, fmt_interval, html_to_lines, json_to_lines,
    keyword_hits, parse_interval, parse_keywords, render_change, render_diff_file,
)

EDGAR_LIKE = """
<html><head><title>EDGAR</title><script>var x = 1;</script></head><body>
<table><tr><td>
  <table>
    <tr><th>Form</th><th>Description</th><th>Accepted</th><th>Filing Date</th><th>File/Film No</th></tr>
    <tr><td><a href="#">S-1</a></td><td colspan="4">ACME ROBOTICS INC (0001234567) (Filer)</td></tr>
    <tr><td>[html] [text]</td><td>General form for registration of securities<br>
        Accession Number: 0001234567-26-000001 Act: 33 Size: 2 MB</td>
        <td>2026-10-15 17:22:11</td><td>2026-10-15</td><td>333-298956<br>261234567</td></tr>
  </table>
</td></tr></table>
<style>.x{color:red}</style>
</body></html>
"""


def test_parse_and_format_interval():
    assert parse_interval("30") == 30
    assert parse_interval("30s") == 30
    assert parse_interval("5m") == 300
    assert parse_interval("5min") == 300
    assert parse_interval("1h30m") == 5400
    assert parse_interval("2d") == 172800
    for bad in ("", "abc", "0", "5x", "m5", "#main"):
        with pytest.raises(ValueError):
            parse_interval(bad)
    assert fmt_interval(5400) == "1h30m"
    assert fmt_interval(45) == "45s"


def test_html_to_lines_flattens_rows_and_drops_scripts():
    lines = html_to_lines(EDGAR_LIKE.encode())
    assert "Form | Description | Accepted | Filing Date | File/Film No" in lines
    assert "S-1 | ACME ROBOTICS INC (0001234567) (Filer)" in lines
    row = [x for x in lines if x.startswith("[html]")][0]
    assert "Accession Number: 0001234567-26-000001 Act: 33 Size: 2 MB" in row
    assert "333-298956 261234567" in row
    blob = "\n".join(lines)
    assert "var x" not in blob and "color:red" not in blob and "EDGAR" not in blob


def test_selector_and_errors():
    page = b"<html><body><div id='a'>um</div><div class='b'>dois</div><p class='b'>tres</p></body></html>"
    assert html_to_lines(page, selector=".b") == ["dois", "tres"]
    assert html_to_lines(page, selector="#a") == ["um"]
    with pytest.raises(ExtractError, match="não encontrou"):
        html_to_lines(page, selector=".nada")
    with pytest.raises(ExtractError, match="inválido"):
        html_to_lines(page, selector="div[[")


def test_json_mode_with_path():
    raw = b'{"data": {"items": [{"id": 2, "n": "b"}, {"id": 1, "n": "a"}]}}'
    lines = json_to_lines(raw, path="data.items.1")
    assert lines == ["{", '  "id": 1,', '  "n": "a"', "}"]
    with pytest.raises(ExtractError, match="não encontrado"):
        json_to_lines(raw, path="data.nope")
    with pytest.raises(ExtractError, match="JSON"):
        json_to_lines(b"<html>")
    assert extract(raw, None, "html", None) == [raw.decode()]


def test_compute_diff_basic():
    old = ["a", "b", "c", "d"]
    new = ["novo", "a", "c", "d", "e"]
    d = compute_diff(old, new)
    assert d.removed == ["b"]
    assert d.added == ["novo", "e"]
    assert compute_diff(old, list(old)).empty


def test_compute_diff_ignore_keeps_original_text():
    old = ["Atualizado 10:00:01", "Preço 10"]
    new = ["Atualizado 10:05:59", "Preço 12"]
    d = compute_diff(old, new, ignore=r"\d{2}:\d{2}:\d{2}")
    assert d.removed == ["Preço 10"] and d.added == ["Preço 12"]
    d2 = compute_diff(["x 1"], ["x 2"], ignore=r"\d+")
    assert d2.empty


def test_keywords():
    kws = parse_keywords(" S-1 , ipo,, ")
    assert kws == ["S-1", "ipo"]
    assert keyword_hits(["NEW IPO filed", "x"], kws) == ["ipo"]
    assert keyword_hits(["nada"], kws) == []


def test_diff_roundtrip():
    d = Diff(["a"], ["b"])
    back, at = Diff.from_json(d.to_json(123.0))
    assert back == d and at == 123.0


def _balanced(text: str) -> bool:
    return text.count("<pre>") == text.count("</pre>") and text.count("<b>") == text.count("</b>")


def test_render_change_small_matches_original_format():
    text, truncated = render_change(
        title="https://www.sec.gov/x", url="https://www.sec.gov/x", when="02/10/2026 02:03:50",
        removed=["linha velha <tag>"], added=["linha nova & cia"],
    )
    assert not truncated
    assert "🔔 <b>ATUALIZAÇÃO DETECTADA!</b>" in text
    assert "📋 <b>Site:</b> https://www.sec.gov/x" in text
    assert "🔗" not in text  # título == url, não repete
    assert "🕐 02/10/2026 02:03:50" in text
    assert "🔴 1 linhas removidas" in text and "🟢 1 linhas adicionadas" in text
    assert "- linha velha &lt;tag&gt;" in text
    assert "+ linha nova &amp; cia" in text
    assert text.index("REMOVIDO") < text.index("ADICIONADO")
    assert _balanced(text)


def test_render_change_truncates_huge_diff_and_prioritizes_added():
    removed = [f"removida {i} " + "x" * 80 for i in range(400)]
    added = [f"adicionada {i}" for i in range(5)]
    text, truncated = render_change(title="Meu site", url="https://a.com", when="agora",
                                    removed=removed, added=added, hits=["IPO"])
    assert truncated
    assert len(text) <= 4096
    assert _balanced(text)
    for i in range(5):
        assert f"+ adicionada {i}" in text      # todas as adicionadas couberam
    assert re.search(r"\+\d+ linhas \(veja o arquivo\)", text)
    assert "🚨 <b>PALAVRA-CHAVE:</b> IPO" in text
    assert "🔗 https://a.com" in text


def test_render_change_giant_lines():
    text, truncated = render_change(title="t", url="u", when="w", removed=["r" * 10000] * 3,
                                    added=["a" * 10000] * 3)
    assert len(text) <= 4096 and truncated and _balanced(text)


def test_render_diff_file():
    out = render_diff_file(title="t", url="u", when="w", removed=["a"], added=["b", "c"])
    assert "RESUMO: 1 linhas removidas / 2 linhas adicionadas" in out
    assert "- a" in out and "+ b" in out and "+ c" in out
