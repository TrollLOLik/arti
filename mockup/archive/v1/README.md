# Арти — «Тёплая связь»

Статичная галерея концепции Telegram Mini App: 12 основных экранов и 4 примера тёмной темы. Весь интерфейс записан в HTML; логики приложения, авторизации, API и обработки сообщений здесь нет. Кнопки внутри телефонов — элементы иллюстрации. Ссылки в шапке перемещают по галерее.

Открыть файл `mockup/index.html` в браузере или перейти на `http://127.0.0.1:8766/` при запущенном статичном сервере этой папки. Для отдельного запуска: `python -m http.server 8766 --directory mockup` из корня проекта.

`index.html` — готовый результат со встроенными стилями. `gallery.css` и `build.py` — исходники. После изменений выполнить `python mockup/build.py`. Все исходники и результаты галереи остаются в `mockup/`; сборка не меняет код приложения или бота.

Изображение `assets/paper-bloom.png` создано встроенным imagegen для этой концепции. Оно используется как иллюстративный образец, а не результат работающей генерации в приложении. Шрифты Unbounded, Onest и JetBrains Mono подключаются через Google Fonts; для их загрузки нужен интернет.

Финальный промпт изображения: “Create one exquisite editorial still life of sculptural paper flowers: a large deep red folded-paper anthurium with a thin golden ochre curved stem, two abstract dusty pink paper petals, and a burgundy paper circle on warm pale rose matte paper background. Minimal art direction, tactile cut paper textures, subtle fibers, precise graceful shadows cast to lower right, refined museum graphic design photography, soft warm side light. Central composition filling a square image, ample pale blush negative space around the shapes. Palette red #F03048, madder #C01830, dusty rose #D87890, pale rose #E4D8D8, dark wine #300000, subtle burnt ochre #E47854. Square. No text, no letters, no watermark, no UI, no frames.”
