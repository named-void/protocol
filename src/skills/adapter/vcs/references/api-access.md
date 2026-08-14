# API хоста: GitLab

Реализация класса `vcs-host` для `vcs_host.product = "gitlab"`; другой продукт
описывается своим файлом этого каталога.

Канал доступа внутри периметра — проектный HTTPS-токен в конфигурации `glab`
(`glab auth status --hostname <HOST>`). Deploy key этой роли не закрывает: он
даёт push, но не API, то есть ни MR, ни discussions. Токен принадлежит
периметру проекта: в карте носителя и в файле
секретов он не хранится.

Используй параметры из MR URL или текущего remote. Не хардкодь host, project, IID или reviewer.

```bash
HOST=gitlab.example.com
PROJECT_PATH=group/project
PROJECT=$(jq -rn --arg value "$PROJECT_PATH" '$value | @uri')
IID=123
REVIEWER=username

glab api --hostname "$HOST" "projects/$PROJECT/merge_requests/$IID"
glab api --hostname "$HOST" "projects/$PROJECT/merge_requests/$IID/changes"
glab api --hostname "$HOST" "projects/$PROJECT/merge_requests/$IID/discussions?per_page=100"
glab api --hostname "$HOST" "projects/$PROJECT/merge_requests/$IID/pipelines?per_page=100"
glab api --hostname "$HOST" "merge_requests?reviewer_username=$REVIEWER&state=opened&scope=all&per_page=100"

BRANCH=feature/ABC-635
glab api --hostname "$HOST" "projects/$PROJECT/merge_requests?source_branch=$BRANCH&state=all&per_page=20"
```

Если ответ содержит следующую страницу, используй `glab api --paginate` и разбирай все страницы структурированным инструментом.

Для self-hosted GitLab всегда передавай `--hostname`. Если `glab api` недоступен из-за TLS, авторизации или несовместимости, используй `curl` с токеном из `glab` config. Не отключай TLS verification, не хардкодь и не выводи токен.

```bash
TOKEN=$(glab config get token --host "$HOST" 2>/dev/null)
BASE="https://$HOST/api/v4/projects/$PROJECT"

curl -sS -H "PRIVATE-TOKEN: $TOKEN" "$BASE/merge_requests/$IID"
curl -sS -H "PRIVATE-TOKEN: $TOKEN" "$BASE/merge_requests/$IID/changes"
curl -sS -H "PRIVATE-TOKEN: $TOKEN" "$BASE/merge_requests/$IID/discussions?per_page=100"
curl -sS -H "PRIVATE-TOKEN: $TOKEN" "$BASE/merge_requests/$IID/pipelines?per_page=100"
```
