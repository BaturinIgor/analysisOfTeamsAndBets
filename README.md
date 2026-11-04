# Анализатор ставок (Dota 2)

Личный инструмент: матчи Dota 2 сегодня и в эфире, собственная оценка вероятностей, сравнение с линией букмекеров, симулятор бумажных ставок. Ставки система не делает. Требования: [docs/requirements.md](docs/requirements.md).

Сейчас готов **этап 0**: проверка источников данных и начало сбора истории коэффициентов.

## Установка на Mac

```bash
git clone https://github.com/BaturinIgor/analysisOfTeamsAndBets.git
cd analysisOfTeamsAndBets
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,browser]'
playwright install chromium
cp .env.example .env
```

Откройте `.env` и вставьте токены: `STRATZ_TOKEN`, `ODDSPAPI_API_KEY` (и `TELEGRAM_BOT_TOKEN` — понадобится на этапе 1). Файл `.env` в git не попадает.

## Команды этапа 0

1. **Проверка источников** — по строке на каждый источник, `[ OK ]` или `[FAIL]` с причиной:
   ```bash
   bk check-sources
   ```
   Пришлите вывод в тред: в нём нет ключей, но видно, есть ли BetBoom, Fonbet и Pari в OddsPapi.

2. **Разведка линии BetBoom** — откроется окно браузера на 120 секунд. Перейдите в киберспорт → Dota 2 и откройте 2–3 матча, лучше один из тех, что идут сейчас. Входить в аккаунт не нужно.
   ```bash
   bk betboom-discover
   bk betboom-inspect data/betboom_discovery/<время>   # краткая сводка: сколько кадров и какие команды в них видны
   ```
   BetBoom получает линию через WebSocket бинарными кадрами (protobuf), они сохраняются как `.bin`. Заархивируйте папку и пришлите в тред — по ней будет написан парсер коэффициентов BetBoom.

3. **Коэффициенты BetBoom**
   ```bash
   bk betboom-parse data/betboom_discovery/<время>   # матчи и П1/П2 из записи разведки
   bk betboom-collect                                # держит линию открытой и пишет каждое изменение коэффициентов в базу (Ctrl+C — стоп)
   ```
   Окно браузера при сборе лучше не сворачивать и не запускать с `--headless`: у BetBoom стоит защита от ботов (Qrator), без видимого окна она чаще показывает капчу. Если капча появилась — пройдите её в этом окне, сбор продолжится.

4. **Сбор коэффициентов OddsPapi в базу** (Fonbet и Pari; BetBoom там нет) — снимок каждые 10 минут в `data/bk.sqlite3`:
   ```bash
   bk collect-odds              # работает, пока открыт терминал
   bk collect-odds --once       # один снимок для проверки
   ```

## Тесты

```bash
pytest
```
