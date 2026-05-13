from crawl.divisare.parsers import parse_project_page


def test_parse_project_page_accepts_plural_designers_label():
    html = """
    <html>
      <body>
        <div class="project">
          <div class="header"><div class="abstract">Short abstract</div></div>
          <div class="sidebar">
            <div class="content">
              <div class="section">Designers</div>
              <div>
                <a href="/authors/11539-aimaro-isola">Aimaro Isola</a>
                <a href="/authors/10016664-luca-moretto">Luca Moretto</a>
              </div>
            </div>
          </div>
          <h1>Mausoleo della Bela Rosin</h1>
        </div>
      </body>
    </html>
    """

    parsed = parse_project_page(
        html,
        "https://divisare.com/projects/5744-aimaro-isola-luca-moretto-mausoleo-della-bela-rosin",
    )

    assert parsed["architect_ids"] == [11539, 10016664]
    assert parsed["architect_names"] == ["Aimaro Isola", "Luca Moretto"]
