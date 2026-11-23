---
name: factory-handoff
description: Übergibt eine fertig besprochene Spec an die Software Factory. Schreibt das Issue ins Produkt-Repo und die Abnahme-Szenarien ins getrennte Szenarien-Repo. Verwenden, wenn eine Idee ausgearbeitet und geprüft ist und als Ticket in die Factory soll.
---

# Übergabe an die Factory

Diese Station steht zwischen der Ideenphase und der automatischen Factory. Aus einer besprochenen und geprüften Spec entstehen zwei Dinge, die getrennt bleiben müssen:

- das **Issue** im Produkt-Repo: Es ist die Spec. Der Implementierer liest es.
- die **Szenarien** im Szenarien-Repo: Gegen sie prüft der Richter den fertigen Stand. Der Implementierer sieht sie nie.

Die Trennung ist der Sinn der Sache. Steht ein Szenario im Issue, in einem Kommentar oder in einer Datei des Produkt-Repos, kann der Implementierer darauf hin programmieren, und das Urteil des Richters sagt nichts mehr.

## Ablauf

1. **Ziel klären.** Frage nach Produkt-Repo und Szenarien-Repo, falls sie nicht aus dem Gespräch hervorgehen. Das Szenarien-Repo steht im Profil des Produkts unter `judge.scenarios_repo` (`.factory/profile.yaml`).

2. **Issue entwerfen** nach der Vorlage des Produkt-Repos (`.github/ISSUE_TEMPLATE/factory-ticket.md`):
   - Was soll sich ändern, aus Sicht des Nutzers
   - Warum
   - Woran erkennt man, dass es fertig ist: beobachtbares Verhalten, auch Fehlerfälle
   - Was gehört nicht dazu

   Das Issue muss für sich stehen. Wer das Gespräch nicht kennt, muss daraus bauen können.

3. **Szenarien entwerfen**, eine Datei je Szenario unter `scenarios/` im Szenarien-Repo. Jedes Szenario ist eine kleine Geschichte:
   - wer der Nutzer ist und was er vorhat
   - was er der Reihe nach tut, mit den genauen Eingaben
   - was ihn zufriedenstellt

   Gute Szenarien prüfen Verhalten von außen und decken auch ab, was das Issue nur andeutet: Randfälle, falsche Eingaben, bestehendes Verhalten, das nicht kaputtgehen darf. Drei bis sechs Szenarien je Ticket reichen meist. Wiederhole nicht einfach die Abnahmekriterien des Issues im Wortlaut.

4. **Beides zeigen und Freigabe einholen.** Lege Issue und Szenarien vor, bevor du etwas anlegst.

5. **Anlegen**, nach der Freigabe:
   - Szenarien im Szenarien-Repo committen und pushen
   - Issue im Produkt-Repo anlegen, ohne Label: `gh issue create --repo OWNER/REPO --title "..." --body-file ...`

6. **Den Start dem Menschen lassen.** Das Label `factory:go` startet die Factory und zählt nur, wenn der Besitzer des Repos es setzt. Nenne die Issue-Nummer und sage, dass das Label der Start ist.

## Worauf achten

- Kein Szenario-Text im Issue, auch nicht als Beispiel.
- Keine Umsetzungsvorgaben im Issue: was und warum, nicht wie.
- Ein Ticket, ein Anliegen. Zwei unabhängige Änderungen werden zwei Issues.
