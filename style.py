"""One stylesheet for both web surfaces — the app (doc/APP.md) and the public
site (doc/LAUNCH.md 4.1).

It lives in a module rather than in either program because the two are
deployed separately and must still look like one product: a visitor moves from
`www.<domain>/gewerke/dachdecker` to `app.<domain>/t/<token>` in one click, and
that click should not feel like leaving.

Constraints that shaped it, both from the specs rather than taste:

- **No external anything.** The app serves under a
  `default-src 'none'` CSP and the public pages must be readable with the app
  down; a web font or a CDN stylesheet would break one or the other. System
  fonts only, one inline `<style>`.
- **No JavaScript**, so no theme toggle: `prefers-color-scheme` does the
  work and both schemes are defined properly rather than inverted.
- **Readable before pretty.** These pages carry a legal notice, a stop
  button and procurement figures; the visual job is to make a table of
  numbers legible on a phone in a site office.

Blue (operator's choice, 2026-08-11) is also the sober end of the palette for
a product whose customer-facing surfaces are a Datenschutzerklärung and an
unsubscribe page.
"""

# Deep blue: dark enough for AA contrast on white at body size, and it stays
# distinguishable from visited-link purple in both schemes.
BLUE = '#1d4ed8'
BLUE_DARK = '#93b4fd'      # the same hue lifted for dark backgrounds

CSS = """
  :root {
    color-scheme: light dark;
    --blue: #1d4ed8; --blue-soft: #eff4ff; --blue-line: #c7d7fe;
    --ink: #0f172a; --muted: #5b6472; --line: #e2e8f0; --bg: #fff;
    --card: #f8fafc;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --blue: #93b4fd; --blue-soft: #16233f; --blue-line: #2b4272;
      --ink: #e8edf5; --muted: #9aa6b6; --line: #263041; --bg: #0d1117;
      --card: #141b26;
    }
  }
  * { box-sizing: border-box }
  body {
    font: 16px/1.65 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: var(--ink); background: var(--bg);
    max-width: 44rem; margin: 0 auto; padding: 2.5rem 1.2rem 4rem;
    -webkit-text-size-adjust: 100%;
  }
  h1 { font-size: 1.5rem; line-height: 1.25; margin: 0 0 .6rem;
       letter-spacing: -.01em }
  h2 { font-size: 1.1rem; margin: 2.2rem 0 .5rem; color: var(--ink) }
  h3 { font-size: .95rem; margin: 1.4rem 0 .3rem; color: var(--muted);
       text-transform: uppercase; letter-spacing: .06em }
  p { margin: .7rem 0 }
  a { color: var(--blue); text-underline-offset: 2px }
  a:hover { text-decoration: none }
  .muted { color: var(--muted); font-size: .93rem }
  .lede { font-size: 1.06rem }

  /* the blue bar that marks every page as ours, header and footer alike */
  header.bar { border-bottom: 3px solid var(--blue); padding-bottom: .7rem;
               margin-bottom: 1.6rem; display: flex; align-items: baseline;
               justify-content: space-between; gap: 1rem; flex-wrap: wrap }
  header.bar .brand { font-weight: 650; color: var(--blue);
                      letter-spacing: -.01em; text-decoration: none }
  header.bar .tag { color: var(--muted); font-size: .85rem }

  dl { display: grid; grid-template-columns: max-content 1fr;
       gap: .35rem 1rem; margin: 1rem 0 }
  dt { color: var(--muted) }
  dd { margin: 0 }

  /* figure cards: the market numbers, legible at a glance */
  .figs { display: grid; gap: .8rem; margin: 1rem 0;
          grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr)) }
  .fig { background: var(--card); border: 1px solid var(--line);
         border-radius: 10px; padding: .8rem .9rem }
  .fig .n { font-size: 1.45rem; font-weight: 650; color: var(--blue);
            line-height: 1.15; display: block }
  .fig .l { color: var(--muted); font-size: .85rem; display: block;
            margin-top: .15rem }

  .note { background: var(--blue-soft); border: 1px solid var(--blue-line);
          border-left: 4px solid var(--blue); border-radius: 8px;
          padding: .85rem 1rem; margin: 1.4rem 0 }
  .note p:first-child { margin-top: 0 } .note p:last-child { margin-bottom: 0 }

  table { border-collapse: collapse; width: 100%; margin: 1rem 0;
          font-size: .93rem }
  th, td { text-align: left; padding: .45rem .6rem;
           border-bottom: 1px solid var(--line) }
  th { color: var(--muted); font-weight: 600; font-size: .85rem;
       text-transform: uppercase; letter-spacing: .04em }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums }
  .scroll { overflow-x: auto }

  ul.plain { list-style: none; padding: 0 }
  ul.plain li { padding: .55rem 0; border-bottom: 1px solid var(--line) }

  input, button { font: inherit }
  input[type=email], input[type=text] {
    width: 100%; padding: .6rem .7rem; border: 1px solid var(--line);
    border-radius: 8px; background: var(--bg); color: var(--ink) }
  input:focus-visible, button:focus-visible, a:focus-visible {
    outline: 2px solid var(--blue); outline-offset: 2px }
  button {
    background: var(--blue); color: #fff; border: 0; border-radius: 8px;
    padding: .6rem 1.15rem; cursor: pointer; font-weight: 550 }
  button:hover { filter: brightness(1.08) }
  @media (prefers-color-scheme: dark) { button { color: #0d1117 } }
  button.secondary { background: transparent; color: var(--blue);
                     border: 1px solid var(--blue-line) }

  footer { margin-top: 3.5rem; padding-top: 1rem;
           border-top: 1px solid var(--line);
           font-size: .875rem; color: var(--muted) }
  footer a { color: var(--muted) }
"""


def header(tagline='Ausschreibungen mit wenig Wettbewerb', home='/'):
    """The blue bar. Same markup on both surfaces — that shared bar is what
    makes the hop from the public site to the app not feel like a hop."""
    return (f'<header class="bar"><a class="brand" href="{home}">'
            f'TenderMining</a><span class="tag">{tagline}</span></header>')


def fig(number, label):
    """One figure card. Numbers are the public site's whole argument, so they
    get the size and the colour and the label stays out of the way."""
    return f'<div class="fig"><span class="n">{number}</span>' \
           f'<span class="l">{label}</span></div>'
