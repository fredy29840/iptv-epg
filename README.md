# iptv-epg

Guide TV fusionné (panel + sources XMLTV publiques), régénéré 4 fois par jour par GitHub Actions
et publié sur GitHub Pages.

- Guide : `https://fredy29840.github.io/iptv-epg/epg.xml.gz`
- Statistiques du dernier passage : `https://fredy29840.github.io/iptv-epg/stats.json`

## Fonctionnement

`merge.py` lit la liste des chaînes du panel, puis remplit chaque chaîne avec la première
source qui la couvre (ordre : panel, puis `SOURCES`). Correspondance par `tvg-id`, sinon par
nom normalisé, sinon par `manual.json` (id de l'appli → id de chaîne d'une source).

Fenêtre : de -6 h à +72 h (`WINDOW_BEFORE_HOURS` / `WINDOW_AFTER_HOURS`).

## Réglages

Secrets du dépôt : `IPTV_HOST`, `IPTV_USER`, `IPTV_PASS`.

Lancement à la main : onglet Actions → EPG → Run workflow, ou `gh workflow run epg.yml`.

Le fichier n'est pas publié s'il contient moins de 25 000 programmes : celui de la veille reste en ligne.
