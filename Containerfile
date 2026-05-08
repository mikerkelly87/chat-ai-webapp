FROM fedora:latest

RUN dnf install -y python3 --setopt=install_weak_deps=False && \
    dnf clean all

WORKDIR /app

COPY server.py index.html ./

RUN mkdir -p chats

EXPOSE 3000

CMD ["python3", "server.py"]
