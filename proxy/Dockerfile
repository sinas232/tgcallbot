# پروکسی سبک HTTP برای درخواست‌های درگاه پرداخت (زرین‌پال/آقای پرداخت).
# این کانتینر روی شبکهٔ عادی داکر است (نه WARP)، پس ترافیکش از IP ایرانیِ
# هاست خارج می‌شود و درگاه‌های ایرانی آن را می‌پذیرند.
FROM alpine:3.19
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
EXPOSE 8888
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
