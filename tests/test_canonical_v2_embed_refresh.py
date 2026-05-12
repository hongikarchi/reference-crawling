import json

from tools import canonical_v2_embed_refresh as embed_refresh


def _emb(seed):
    return [float(seed)] * 384


def test_refresh_embeddings_copies_unaffected_and_encodes_affected(tmp_path):
    strict_path = tmp_path / "strict.json"
    base_path = tmp_path / "base.json"
    affected_path = tmp_path / "affected.json"
    output_path = tmp_path / "out.json"

    strict_path.write_text(
        json.dumps(
            {
                "buildings": [
                    {"canonical_bld_id": "bld_keep", "name": "Keep House"},
                    {"canonical_bld_id": "bld_new", "name": "New House"},
                ]
            }
        ),
        encoding="utf-8",
    )
    base_path.write_text(
        json.dumps({"buildings": [{"canonical_bld_id": "bld_keep", "embedding": _emb(1)}]}),
        encoding="utf-8",
    )
    affected_path.write_text(json.dumps({"affected_cids": ["bld_new"]}), encoding="utf-8")

    calls = []

    def fake_encoder(texts):
        calls.append(list(texts))
        return [_emb(2) for _ in texts]

    report = embed_refresh.refresh_embeddings(
        input_path=strict_path,
        base_path=base_path,
        affected_path=affected_path,
        output_path=output_path,
        encoder=fake_encoder,
    )

    assert report["status"] == "PASS"
    assert report["copied_embeddings"] == 1
    assert report["encoded_embeddings"] == 1
    assert calls == [["New House"]]

    rows = {
        row["canonical_bld_id"]: row
        for row in json.loads(output_path.read_text(encoding="utf-8"))["buildings"]
    }
    assert rows["bld_keep"]["embedding"] == _emb(1)
    assert rows["bld_new"]["embedding"] == _emb(2)


def test_refresh_embeddings_reencodes_affected_even_if_base_has_embedding(tmp_path):
    strict_path = tmp_path / "strict.json"
    base_path = tmp_path / "base.json"
    affected_path = tmp_path / "affected.json"
    output_path = tmp_path / "out.json"

    strict_path.write_text(json.dumps({"buildings": [{"canonical_bld_id": "bld_changed", "name": "Changed"}]}), encoding="utf-8")
    base_path.write_text(json.dumps({"buildings": [{"canonical_bld_id": "bld_changed", "embedding": _emb(1)}]}), encoding="utf-8")
    affected_path.write_text(json.dumps({"affected_cids": ["bld_changed"]}), encoding="utf-8")

    report = embed_refresh.refresh_embeddings(
        input_path=strict_path,
        base_path=base_path,
        affected_path=affected_path,
        output_path=output_path,
        encoder=lambda texts: [_emb(9) for _ in texts],
    )

    row = json.loads(output_path.read_text(encoding="utf-8"))["buildings"][0]
    assert report["encoded_embeddings"] == 1
    assert row["embedding"] == _emb(9)
