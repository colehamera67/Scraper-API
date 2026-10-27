from app import cli
from app.history import PriceStore


def test_ingest_feed_and_drops(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_store", lambda: PriceStore(tmp_path / "p.db"))
    feed = tmp_path / "feed.csv"
    feed.write_text("id,name,price,retailer\n1,Drill,50,walmart\n2,Saw,30,lowes\n")
    assert cli.main(["ingest-feed", str(feed), "--source", "test"]) == 0
    assert "2 products, 2 new price observations, 0 bad rows" in capsys.readouterr().out
    assert cli.main(["drops"]) == 0
    assert "no drops found" in capsys.readouterr().out
