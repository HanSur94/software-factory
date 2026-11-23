# Sicherheit

Die Software Factory lässt Agents Code in fremden Repos schreiben. Lücken, durch die ein Agent, ein Issue oder ein Pull Request an Tokens, Secrets oder fremde Runner kommt, nehmen wir ernst.

## Eine Lücke melden

Bitte **kein öffentliches Issue**. Melde sie privat über [Report a vulnerability](https://github.com/HanSur94/software-factory/security/advisories/new) auf GitHub. Beschreibe, wie man sie auslöst und was jemand damit erreichen kann. Wir antworten so bald wie möglich und sagen dir, wie es weitergeht.

## Was im Rahmen ist

- Agents oder Checks, die an `GITHUB_TOKEN`, Claude-Zugang oder andere Secrets kommen
- Wege, auf denen ein Issue oder Pull Request Code auf einem selbst gehosteten Runner ausführt, ohne dass der Besitzer zustimmt
- Änderungen an `.github/` oder `.factory/`, die trotz Sperre durchkommen
- Szenario-Text des Richters, der beim Implementierer ankommt

## Hinweis für Betreiber

Selbst gehostete Runner gehören nur an private Repos oder an Repos, deren Workflows keine Pull Requests aus Forks ausführen. Die Factory selbst prüft ihre Tests auf GitHubs eigenen Runnern.
