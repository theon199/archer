FROM python@sha256:c3729439afc4ec3f3e159e0c86aeabfadac8030ef46838a0256383d0511c5f5e
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /build/requirements.txt
COPY app /opt/archer/app
COPY dist /opt/archer/frontend
COPY sqlite/lib/libsqlite3.so.0 /opt/archer/sqlite/lib/libsqlite3.so.0
COPY sqlite/sqlite-runtime.json /opt/archer/sqlite/sqlite-runtime.json
WORKDIR /opt/archer
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LD_LIBRARY_PATH=/opt/archer/sqlite/lib
RUN python -c "import sqlite3; assert sqlite3.sqlite_version == '3.54.1' and sqlite3.threadsafety == 3"
