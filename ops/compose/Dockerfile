ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY vendor/ /usr/local/lib/python3.12/site-packages/
COPY wheel/ /tmp/jimu-wheel/
RUN python -m pip install --no-index --no-deps --no-cache-dir /tmp/jimu-wheel/*.whl \
    && rm -rf /tmp/jimu-wheel \
    && install -d -m 0700 -o 10001 -g 10001 /var/lib/jimu
COPY host/ /opt/jimu/host/
RUN chmod -R a+rX /opt/jimu/host /usr/local/lib/python3.12/site-packages
WORKDIR /opt/jimu/host
USER 10001:10001
RUN python -I -c 'import greenlet, PIL._imaging, psycopg, pydantic_core, rpds; from jimu_runtime.deployment import service; assert psycopg.pq.__impl__ == "binary"'
