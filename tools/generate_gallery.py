import json
import os

def generate_html():
    print("Loading data/enrich/4_buildings_final.json...")
    with open('data/enrich/4_buildings_final.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    html_content = """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Architecture Database Visual Check</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f5f7; padding: 30px; margin: 0; }
            h1 { text-align: center; color: #333; margin-bottom: 40px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 24px; max-width: 1400px; margin: 0 auto; }
            .card { background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05); transition: transform 0.2s; border: 1px solid #eaeaea; }
            .card:hover { transform: translateY(-5px); box-shadow: 0 8px 15px rgba(0,0,0,0.1); }
            .image-container { width: 100%; height: 220px; background: #eee; overflow: hidden; position: relative; }
            .image-container img { width: 100%; height: 100%; object-fit: cover; }
            .info { padding: 20px; }
            .title { font-size: 1.1em; font-weight: 700; margin: 0 0 8px 0; color: #1a1a1a; line-height: 1.3; }
            .architect { font-size: 0.9em; color: #666; margin: 0 0 16px 0; display: flex; align-items: center; gap: 6px; }
            .tags { display: flex; flex-wrap: wrap; gap: 6px; }
            .tag { background: #f0f0f0; padding: 4px 10px; border-radius: 6px; font-size: 0.75em; color: #444; font-weight: 500; }
            .empty-tag { color: #aaa; font-size: 0.8em; font-style: italic; }
        </style>
    </head>
    <body>
        <h1>건축물 DB 시각적 검수 갤러리</h1>
        <div class="grid">
    """

    # 성능을 위해 상위 500개만 렌더링 (원하면 늘릴 수 있음)
    items_to_show = data[:500]
    print(f"Generating gallery for {len(items_to_show)} items...")
    
    for idx, item in enumerate(items_to_show):
        building_id = item.get('building_id', '')
        slug = item.get('slug', '')
        
        # 제목 우선순위
        title = item.get('project_name') or item.get('name_en') or item.get('title') or '제목 없음 (Unknown)'
        
        # 건축가
        architect = item.get('architect') or '건축가 미상 (Unknown)'
        
        # 태그 (문자열 리스트라고 가정)
        tags = item.get('tags', [])
        if not tags and item.get('categories'):
            tags = item.get('categories')
            
        tags_html = ""
        if tags and isinstance(tags, list):
            tags_html = "".join([f'<span class="tag">#{t}</span>' for t in tags[:5]]) # 최대 5개만
            if len(tags) > 5:
                tags_html += f'<span class="tag">+{len(tags)-5}</span>'
        else:
            tags_html = '<span class="empty-tag">태그 없음</span>'

        # 이미지 찾기
        images = item.get('images', [])
        img_src = ""
        if images:
            # photo 타입 우선, 없으면 첫번째
            first_img = next((img for img in images if img.get('type') == 'photo'), images[0])
            filename = first_img.get('filename', '')
            
            # images/ 빌딩ID 또는 slug 폴더 내에 있는지 확인
            possible_paths = [
                f"images/{building_id}/{filename}",
                f"images/{slug}/{filename}"
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    img_src = path
                    break
            
            # 파일이 없더라도 일단 building_id 경로로 세팅 (HTML에서 엑박으로 표시되도록)
            if not img_src:
                img_src = f"images/{building_id}/{filename}"

        card = f"""
        <div class="card">
            <div class="image-container">
                <img src="{img_src}" alt="{title}" loading="lazy" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'300\\' height=\\'220\\'%3E%3Crect width=\\'300\\' height=\\'220\\' fill=\\'%23f8f8f8\\'/%3E%3Ctext x=\\'50%25\\' y=\\'50%25\\' dominant-baseline=\\'middle\\' text-anchor=\\'middle\\' font-family=\\'sans-serif\\' font-size=\\'14px\\' fill=\\'%23aaa\\'%3EImage Not Found%3C/text%3E%3C/svg%3E'">
            </div>
            <div class="info">
                <h3 class="title">{title}</h3>
                <p class="architect">🏛 {architect}</p>
                <div class="tags">
                    {tags_html}
                </div>
            </div>
        </div>
        """
        html_content += card

    html_content += """
        </div>
    </body>
    </html>
    """

    with open('gallery.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print("✅ Successfully created 'gallery.html'!")

if __name__ == '__main__':
    generate_html()
