# Kryptoradar · Revolut

Sender push-varsel til mobilen (ntfy) når en kryptovaluta i Revolut stiger mer enn 20 % på 24 timer eller 1 time. Hver mynt som utløser et varsel får en egen analyseside med graf og hurtigsjekk.

## Slik virker det

- `varsel.py` henter alle kryptovalutaer Revolut tilbyr i Norge (priser i NOK) hvert 15. minutt via GitHub Actions.
- Stablecoins og mynter med mindre enn 1 mill. kr i volum på 24 t ignoreres.
- Samme mynt varsles ikke på nytt før det har gått 12 timer, med mindre den har steget ytterligere 15 prosentpoeng.
- Hvis Revolut ikke svarer, brukes CoinGecko som reserve, og du får beskjed.
- Analysepanelet (`docs/index.html`) publiseres på GitHub Pages.

## Endre innstillinger

Under **Settings → Secrets and variables → Actions → Variables** kan du legge inn:

| Variabel | Standard | Betydning |
|---|---|---|
| `TERSKEL_24T` | 20 | % stigning på 24 t som gir varsel |
| `TERSKEL_1T` | 20 | % stigning på 1 t som gir varsel |
| `MIN_VOLUM_NOK` | 1000000 | Minste handelsvolum på 24 t |

Secret `NTFY_TOPIC` er navnet på ntfy-emnet mobilen abonnerer på.

## Testvarsel

**Actions → Kryptovarsel → Run workflow**, huk av «Send testvarsel».

> Dette er et analyseverktøy, ikke investeringsråd. Revolut-dataene kommer fra et uoffisielt endepunkt som kan endres uten forvarsel.
