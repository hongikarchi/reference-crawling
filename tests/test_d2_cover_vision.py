import json

from tools import d2_cover_vision


def test_d2_picks_best_cover_and_writes_mocked_vision_payload(tmp_path):
    canonical_path = tmp_path / "canonical.json"
    e1_path = tmp_path / "e1.jsonl"
    output_path = tmp_path / "d2.jsonl"
    canonical_path.write_text(
        json.dumps(
            {
                "clusters": [
                    {"canonical_bld_id": "bld_cover"},
                    {"canonical_bld_id": "bld_missing"},
                ]
            }
        ),
        encoding="utf-8",
    )
    e1_path.write_text(
        json.dumps(
            {
                "cid": "bld_cover",
                "all_images": [
                    {
                        "url": "https://img.test/gallery.jpg",
                        "kind": "gallery",
                        "source": "divisare",
                        "image_order": 1,
                        "w": 2000,
                        "h": 1200,
                    },
                    {
                        "url": "https://img.test/cover.jpg",
                        "kind": "cover",
                        "source": "archello",
                        "image_order": 0,
                        "w": 800,
                        "h": 600,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []

    def fake_classifier(url):
        calls.append(url)
        return {
            "style_image": "contemporary",
            "color_tone_image": "neutral",
            "material_visual_image": ["concrete", "glass"],
            "visual_description_image": "A compact contemporary building presents a quiet facade with concrete and glass surfaces.",
        }

    summary = d2_cover_vision.run_all(
        canonical_path=canonical_path,
        e1_path=e1_path,
        output_path=output_path,
        workers=2,
        classifier=fake_classifier,
    )

    assert summary["rows_processed"] == 2
    assert summary["rows_with_cover"] == 1
    assert calls == ["https://img.test/cover.jpg"]

    rows = {
        row["cid"]: row
        for row in (json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines())
    }
    assert rows["bld_cover"]["cover_url"] == "https://img.test/cover.jpg"
    assert rows["bld_cover"]["style_image"] == "Contemporary"
    assert rows["bld_cover"]["color_tone_image"] == "Neutral"
    assert rows["bld_cover"]["material_visual_image"] == ["concrete", "glass"]
    assert rows["bld_missing"]["cover_url"] is None
    assert rows["bld_missing"]["visual_description_image"] is None
