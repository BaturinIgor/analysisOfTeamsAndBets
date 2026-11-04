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

2. **Разведка линии BetBoom** — откроется окно браузера на 90 секунд. Перейдите в киберспорт → Dota 2 и откройте пару матчей. Входить в аккаунт не нужно.
   ```bash
   bk betboom-discover --seconds 90
   ```
   Ответы сайта сохранятся в `data/betboom_discovery/<время>/`. Заархивируйте эту папку и пришлите в тред — по ней будет написан парсер коэффициентов BetBoom.

3. **Сбор коэффициентов в базу** (если OddsPapi покрывает нужные БК) — снимок каждые 10 минут в `data/bk.sqlite3`:
   ```bash
   bk collect-odds              # работает, пока открыт терминал
   bk collect-odds --once       # один снимок для проверки
   ```

## Тесты

```bash
pytest
```
