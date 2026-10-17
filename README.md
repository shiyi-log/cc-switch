# CC Switch

Django 6 project managed with [uv](https://docs.astral.sh/uv/). Requires Python 3.12+.

## 快速开始

```bash
uv sync                              # 创建 .venv 并安装依赖
uv run python manage.py migrate      # 初始化数据库 (SQLite)
uv run python manage.py runserver    # http://127.0.0.1:8000/
```

## 常用命令

```bash
uv add <package>                # 添加依赖
uv run python manage.py startapp <name>
uv run python manage.py check
uv run python manage.py createsuperuser
```

## 目录结构

```
manage.py
cc_switch/          # Django 项目配置 (settings / urls / asgi / wsgi)
pyproject.toml      # 项目元数据与依赖
uv.lock             # 依赖锁文件
.python-version     # 固定 Python 3.12
```
