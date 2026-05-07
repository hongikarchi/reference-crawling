import json

from tools import e2_vision_5type


def test_e2_uses_filename_heuristic_and_mocked_classifier(tmp_path):
    input_path = tmp_path / "e1.jsonl"
    output_path = tmp_path / "e2.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "cid": "bld_test",
                "best_image_per_cluster": {
                    "0": {
                        "url": "https://img.test/floor-plan.jpg",
                        "kind": "gallery",
                        "source": "divisare",
                        "image_order": 1,
                        "rank": 0,
                    },
                    "1": {
                        "url": "https://img.test/living-room.jpg",
                        "kind": "gallery",
                        "source": "archello",
                        "image_order": 2,
                        "rank": 0,
                    },
                },
                "all_images": [
                    {
                        "url": "https://img.test/floor-plan.jpg",
                        "kind": "gallery",
                        "source": "divisare",
                        "image_order": 1,
                        "phash_cluster_id": 0,
                        "rank": 0,
                    },
                    {
                        "url": "https://img.test/living-room.jpg",
                        "kind": "gallery",
                        "source": "archello",
                        "image_order": 2,
                        "phash_cluster_id": 1,
                        "rank": 0,
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
        return "interior"

    summary = e2_vision_5type.run_all(
        input_path=input_path,
        output_path=output_path,
        workers=2,
        classifier=fake_classifier,
    )

    assert summary["rows_processed"] == 1
    assert calls == ["https://img.test/living-room.jpg"]
    row = json.loads(output_path.read_text(encoding="utf-8"))
    assert row["image_types"] == {"0": "drawing", "1": "interior"}
    assert row["covers_by_type"]["drawing"] == "https://img.test/floor-plan.jpg"
    assert row["covers_by_type"]["interior"] == "https://img.test/living-room.jpg"
    assert row["all_images_with_type"][0]["type"] == "drawing"
