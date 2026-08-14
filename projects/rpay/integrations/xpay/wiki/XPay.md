# XPay: выжимка контрактов для MONELIQ-1

Снято 2026-08-05 из публичной документации XPay. Это локальная выжимка для
планирования; при реализации сверять детали полей с первоисточниками.

## Базовый API

- Production API: `https://api.gateway.xpate.com/v1`; токенизация карт —
  `https://tokens.gateway.xpate.com`. Аутентификация в примерах — Basic.
- Новый заказ: `POST /orders`; сумма передаётся в минимальных единицах валюты.
  Для HPP ответ содержит `order_url`/`payment_url` для перенаправления клиента.
- Для H2H 3-D Secure выполняется отдельным
  `POST /orders/{order_id}/transactions/{transaction_id}/authenticate/`; ответ
  содержит `redirect_url`.

## Сценарии в объёме задачи

- Cards: H2H, refund и webhook.
- Apple Pay: HPP/Redirect и H2H. Для H2H с зашифрованным payload XPay описывает
  создание заказа, создание Apple Pay session, сохранение payment token и
  выполнение платежа через URL транзакции.
- Google Pay: HPP/Redirect и H2H. H2H использует платёжный token Google Pay,
  создание заказа и выполнение платежа через URL транзакции.
- Refund: `POST /orders/{id}/refunds`; `amount` задаёт полный либо частичный
  возврат, валюта наследуется от исходного заказа.
- Статус: `GET /orders/{order_id}`. Для HPP учитывается статус заказа, для H2H
  — статус единственной транзакции.

## Webhook

- URL задаётся на уровне проекта или полем `webhook_url` при создании заказа.
- Событие статуса приходит HTTP `POST` с `event`, `order_id` и `project_id`.
- Получатель подтверждает обработку HTTP `200`; при ошибке или таймауте XPay
  повторяет доставку до десяти раз с интервалом две минуты.
- На прочитанной странице не описана подпись webhook. Этот контракт остаётся
  открытым и не должен предполагаться.

## Первичные источники

- https://developer.xpate.com/llms.txt
- https://developer.xpate.com/reference/introduction.md
- https://developer.xpate.com/docs/host-to-host.md
- https://developer.xpate.com/docs/hosted-payment-page.md
- https://developer.xpate.com/docs/apple-pay.md
- https://developer.xpate.com/docs/google-pay.md
- https://developer.xpate.com/docs/refund-payments.md
- https://developer.xpate.com/docs/status-requests.md
- https://developer.xpate.com/docs/webhooks.md
- https://developer.xpate.com/reference/post_orders.md
- https://developer.xpate.com/reference/post_orders-id-refunds.md
