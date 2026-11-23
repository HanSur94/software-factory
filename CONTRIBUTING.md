# Mitmachen

Danke, dass du die Software Factory besser machen willst. Fragen, Fehler und Ideen sind als Issue willkommen, Änderungen als Pull Request.

## So läuft ein Beitrag

1. Für größere Änderungen erst ein Issue öffnen und kurz beschreiben, was du vorhast.
2. Forken, einen Branch anlegen und die Änderung bauen.
3. Die Tests laufen lassen, ohne Modellaufrufe und in wenigen Minuten:

   ```bash
   pip install -r requirements.txt pytest
   python3 -m pytest -q
   ```

4. Pull Request öffnen und beschreiben, was sich ändert und warum.

## Worauf wir achten

- **Allgemein bleiben:** Die Factory läuft für beliebige Projekte. Nichts im Code darf ein bestimmtes Projekt, einen Rechner oder einen Nutzer voraussetzen; Projektwissen gehört ins Profil des Projekts (`.factory/profile.yaml`).
- **Das Steuerskript entscheidet, nicht ein Agent:** Reihenfolge, Runden, Budgets und Parallelität bleiben im Python-Code (`factory/pipeline.py`), damit sie vorhersagbar und testbar sind.
- **Tests zu jeder Änderung:** Neues Verhalten bekommt einen Test unter `tests/`. Die Tests nutzen einen falschen `gh` und falsche Agents, nie echte Modelle.
- **Sicherheit zuerst:** Agents bekommen keine Tokens, und Änderungen an `.github/` und `.factory/` werden verworfen. Ein Beitrag, der diese Grenzen lockert, braucht eine gute Begründung.
- **Texte:** Board und README sind auf Deutsch, Code-Kommentare auf Englisch. Kurze, klare Sätze.

## Lizenz

Mit einem Beitrag stimmst du zu, dass er unter der [MIT-Lizenz](LICENSE) des Projekts steht.
