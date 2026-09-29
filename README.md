# Hermes VK Adapter

Плагин подключает личные сообщения сообщества ВКонтакте к Hermes Agent. Он получает события через VK Bots Long Poll и отправляет ответы через VK API. Публичный IP, входящий порт и Callback API не нужны.

```text
Пользователь VK → Сообщество VK → VK Bots Long Poll → Плагин → Hermes Agent
Пользователь VK ← Сообщество VK ← VK messages.send ← Плагин ← Ответ Hermes
```

## Возможности и совместимость

- Личные текстовые сообщения, ответы в тот же диалог и обычные сессии Hermes.
- Ограничение входящих сообщений и исходящих ответов списком `VK_ALLOWED_USERS`.
- Повторное подключение Long Poll и сохранение его курсора между перезапусками.
- Разделение длинных ответов на сообщения допустимого размера.

Групповые беседы, вложения и другие события VK не поддерживаются. Интеграция проверена на установленном Hermes Agent **0.21.0**, revision `37f3ba110a1b537fe261d1e64c479fb37b3119af`, с VK API **5.199**. Другие версии Hermes не проверены. На этом экземпляре platform plugin был зарегистрирован, реальное сообщение прошло VK → Hermes → VK, повторное сообщение сохранило сессию, а подключение восстановилось после перезапуска контейнера.

## 1. Подготовь сообщество VK

1. Включи сообщения сообщества.
2. Создай **ключ доступа сообщества** с разрешениями **messages** и **manage**. В проверенной установке ключ только с `messages` давал ошибку VK `15` (subcode `1133`) при `groups.getLongPollServer`.
3. Включи Long Poll API и событие **«Входящее сообщение»**. Для Unified Identity + Delivery v1 дополнительно включи **«Действие с сообщением»** (`message_event`): это событие доставляет нажатие кнопки `✅ Принял`. Проверенная версия VK API — `5.199`.
4. Узнай числовой ID сообщества и числовые ID пользователей, которым разрешён доступ к Hermes.

![Разрешения ключа сообщества](docs/images/03-create-token-permissions.png)
![Включённый Long Poll](docs/images/04-long-poll-enabled.png)
![Событие входящего сообщения](docs/images/05-long-poll-message-event.png)
![Пример сообщества Hermes AI](docs/images/01-community-hermes-ai.png)
![Включённые сообщения сообщества](docs/images/02-community-messages-enabled.png)

## 2. Найди постоянное хранилище Hermes в Docker

Выполняй команды на Docker-хосте. Замени значения в угловых скобках фактическими именами своей установки. Имя контейнера можно найти командой `docker ps --format '{{.Names}}'`.

```sh
CONTAINER='<имя-контейнера-Hermes>'
docker exec "$CONTAINER" printenv HERMES_HOME
docker inspect "$CONTAINER" --format '{{range .Mounts}}{{println .Type .Name .Source "->" .Destination}}{{end}}'
docker inspect "$CONTAINER" --format '{{index .Config.Labels "com.docker.compose.service"}}'
docker inspect "$CONTAINER" --format '{{index .Config.Labels "com.docker.compose.project"}}'
docker inspect "$CONTAINER" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker inspect "$CONTAINER" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

Первая команда показывает путь `HERMES_HOME` **внутри контейнера**. В списке mounts найди bind mount или Docker volume, чей `Destination` равен этому пути или является его родительским каталогом. `Source` у bind mount — путь на Docker-хосте; для named volume ориентируйся на его `Name`. Compose labels показывают service, project, Compose-файлы и рабочий каталог. Если конфигурация использует несколько `-f` или отдельный `--env-file`, укажи их в командах пересоздания в том же порядке, что и при обычном запуске. Если Compose labels отсутствуют, выясни эти параметры из конфигурации своей установки. Если `HERMES_HOME` не задан или каталог не покрыт постоянным mount, сначала настрой постоянное хранилище в существующей Docker-конфигурации и повтори проверку. Не устанавливай плагин только в файловый слой контейнера: при recreate он исчезнет.

## 3. Установи плагин в постоянный каталог

Склонируй репозиторий на Docker-хост и скопируй файлы плагина **в смонтированный** `$HERMES_HOME/plugins/vk-platform`. Укажи адрес этого репозитория вместо шаблона и точный путь из шага 2. `docker cp` пишет в mount, если целевой каталог находится внутри подтверждённого постоянного mount.

```sh
git clone 'https://github.com/LuckDMST/hermes-vk-adapter.git' hermes-vk-adapter
HERMES_HOME_IN_CONTAINER='<значение-HERMES_HOME-из-шага-2>'
docker exec "$CONTAINER" mkdir -p "$HERMES_HOME_IN_CONTAINER/plugins/vk-platform"
for file in plugin.yaml __init__.py adapter.py; do
  docker cp "hermes-vk-adapter/$file" "$CONTAINER:$HERMES_HOME_IN_CONTAINER/plugins/vk-platform/$file"
done
docker exec "$CONTAINER" test -f "$HERMES_HOME_IN_CONTAINER/plugins/vk-platform/plugin.yaml"
```

При bind mount можно вместо `docker cp` скопировать файлы прямо в соответствующий каталог на Docker-хосте. Проверь, что `plugin.yaml` находится именно в `$HERMES_HOME/plugins/vk-platform/plugin.yaml` внутри контейнера. Сохрани существующие каталоги других плагинов.

## 4. Передай настройки контейнеру

Адаптер читает переменные окружения процесса Hermes:

| Переменная | Значение |
| --- | --- |
| `VK_TOKEN` | Секретный ключ доступа **этого сообщества** с `messages` и `manage`. |
| `VK_GROUP_ID` | Числовой ID сообщества, которому принадлежит ключ. |
| `VK_ALLOWED_USERS` | Разделённые запятыми числовые ID разрешённых пользователей VK; хотя бы один ID обязателен. |
| `VK_STATE_DIR` | Необязательный абсолютный путь к каталогу курсора Long Poll внутри контейнера. Поддерживается `~` домашнего каталога процесса. |

По умолчанию курсор хранится в `get_hermes_home()/plugin-data/vk-platform/vk-long-poll-<group_id>.json`, где `get_hermes_home()` — функция конфигурации Hermes 0.21.0. В проверенной Docker-установке `HERMES_HOME=/opt/data`, и этот каталог покрыт постоянным bind mount. Убедись, что `plugin-data` тоже находится в постоянном mount: плагин и курсор должны переживать recreate контейнера.

При обновлении со старой версии, если нового файла нет, адаптер читает прежний курсор сначала из `get_hermes_home()/vk-long-poll-<group_id>.json`, затем из `/opt/data/vk-long-poll-<group_id>.json`. Второй путь нужен только для миграции версии, которая жёстко записывала курсор в `/opt/data`, даже при другом `HERMES_HOME`. Если оба старых пути совпадают, файл читается один раз. Новая запись всегда идёт в `plugin-data`; старые файлы не изменяются. Если нужен другой каталог, задай `VK_STATE_DIR` как абсолютный путь **внутри контейнера** к постоянному хранилищу. С override старые файлы не читаются. Относительный путь отклоняется; перед записью адаптер создаёт каталог с закрытыми правами для нового каталога и атомарно сохраняет файл курсора.

Добавь переменные в **существующее** определение Hermes service в Compose, `env_file` или используемый механизм передачи секретов. Например, если Compose уже получает значения из локального `.env` или хранилища секретов, в `environment` сервиса укажи:

```yaml
environment:
  VK_TOKEN: ${VK_TOKEN}
  VK_GROUP_ID: ${VK_GROUP_ID}
  VK_ALLOWED_USERS: ${VK_ALLOWED_USERS}
```

Значения помести в существующий защищённый источник окружения. Плагин читает именно переменные окружения, поэтому секрет, доступный только как файл Docker secrets, нужно передать ему через предусмотренный в твоей конфигурации механизм переменных. Не добавляй реальный токен в `compose.yaml`, Git, историю shell или отчёты команд. Пример формата без реальных значений находится в [.env.example](.env.example).

После изменения окружения **пересоздай контейнер** через тот же Compose project и файл, которыми он управляется. Простой `docker restart` не применяет новые переменные. Определи Compose service командой из шага 2 или `docker compose -f '<путь-к-существующему-compose.yaml>' config --services`.

```sh
COMPOSE_FILE='<путь-к-существующему-compose.yaml>'
SERVICE='<имя-сервиса-Hermes-в-этом-Compose>'
COMPOSE_PROJECT='<имя-project-из-label-контейнера>'
docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$SERVICE"
```

Если Hermes управляется не Compose, используй механизм пересоздания контейнера своей установки с сохранением всех существующих mounts, настроек и каналов. После recreate уточни текущее имя через `docker ps`; `CONTAINER` должен указывать на новый работающий контейнер.

## 5. Включи плагин и запусти канал

В проверенной версии Hermes CLI плагин включается так:

```sh
docker exec "$CONTAINER" hermes plugins enable vk-platform
```

Если gateway работал до включения плагина, перезапусти Hermes штатным способом своей установки, чтобы он загрузил платформу. В Compose это `docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" restart "$SERVICE"`. После запуска проверь статус Hermes: gateway должен работать, а платформа `vk` — быть подключена. Если подключения нет, проверь ошибки gateway, права ключа, соответствие `VK_GROUP_ID`, Long Poll и allowlist. Перед передачей логов другим людям убедись, что в них нет секретов.

## 6. Проверь реальный обмен

1. Отправь личное текстовое сообщение сообществу от пользователя из `VK_ALLOWED_USERS` и дождись ответа в том же диалоге.
2. Отправь второе сообщение и проверь, что Hermes использует прежний контекст сессии.
3. Проверь, что сообщение от пользователя вне allowlist не обрабатывается.
4. Перезапусти или пересоздай контейнер штатным способом, дождись подключения `vk` и отправь ещё одно сообщение. Проверь ответ и сохранение контекста.
5. Если установлен Unified Identity + Delivery v1, проверь в логах `Unified delivery enabled: VK message_event verified`. Эта проверка читает `groups.getLongPollSettings` и подтверждает включение `events.message_event`, не выводя токен. Затем нажми `✅ Принял` на реальном VK-уведомлении и проверь подтверждение. Сообщение `Unified delivery disabled` в логах указывает на старый адаптер, отключённое событие или невозможность проверить настройку.

Скриншоты проверенного обмена и статуса подключения приведены ниже. Локальные тесты не заменяют проверку с твоим сообществом и реальным Hermes.

![Первый обмен VK и Hermes](docs/images/06-vk-first-message.png)
![Сессия после перезапуска](docs/images/07-session-after-restart.png)
![Подключённая платформа VK](docs/images/08-hermes-vk-connected.png)

## Устранение неполадок

- `groups.getLongPollServer` возвращает `15` / `1133`: проверь, что это ключ **сообщества**, принадлежащий `VK_GROUP_ID`, с разрешениями `messages` и `manage`. В проверенной установке причиной была нехватка `manage`.
- Подключение есть, входящих сообщений нет: проверь сообщения сообщества, Long Poll и событие «Входящее сообщение».
- `Unified delivery disabled` при работающих обычных VK-сообщениях: проверь событие «Действие с сообщением» (`message_event`) и версию адаптера с `set_message_event_handler`/`message_event_enabled`. Путь установки зависит от постоянного каталога данных конкретной установки.
- Сообщение игнорируется: проверь числовой ID отправителя в `VK_ALLOWED_USERS`.
- После recreate плагин пропал: проверь, что `$HERMES_HOME/plugins/vk-platform` расположен в постоянном bind mount или volume.
- Сообщение `Redirected current run` создаётся Hermes gateway при перенаправлении запуска. В проверенном контракте исходящей отправки нет подтверждённого признака, по которому плагин мог бы безопасно отличить его от текста ответа. Фильтрация по фразе в адаптер не добавлена.

## Разработка

Локальные тесты адаптера:

```sh
python -m unittest -v test_adapter.py
```

Проверка контракта требует окружения, в котором доступны пакеты установленного Hermes:

```sh
python smoke_hermes.py
```

Эти проверки не подтверждают успешный обмен VK → Hermes → VK. Для него нужны действующий ключ сообщества и шаги из раздела выше.

## Лицензия

MIT, см. [LICENSE](LICENSE).

## VK callback и ACK transport

Этот раздел описывает только транспортные возможности VK adapter. Generic identity mapping и delivery policy остаются в отдельном `hermes-unified-identity`.

- Для кнопок ACK включи в настройках сообщества Long Poll событие «Действие с сообщением» (`message_event`) дополнительно к «Входящему сообщению» (`message_new`). Adapter при запуске проверяет `groups.getLongPollSettings` → `events.message_event`.
- Callback keyboard отправляется через `messages.send` как inline keyboard с callback action и JSON payload. Callback принимается только от allowlisted пользователя в личном peer и передаётся зарегистрированному handler.
- Чтобы получить обычный API message ID из `conversation_message_id` события, adapter вызывает `messages.getByConversationMessageId`.
- Для подтверждения обработанного callback вызывается `messages.sendMessageEventAnswer`; VK показывает snackbar «Принято».
- `edit_message(..., clear_keyboard=True)` использует `messages.edit` и очищает клавиатуру сообщения. `delete_message` использует `messages.delete` с `delete_for_all=1`.
- Нужен community access token с правами `messages` и `manage`; `manage` требуется для чтения Long Poll settings. Секрет токена не включай в файлы проекта или логи.

VK adapter предоставляет transport API; управление identity, состояниями уведомления, ACK policy и fallback выполняется отдельным generic расширением.
