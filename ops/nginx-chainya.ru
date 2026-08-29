# chainya.ru — сайт и checkout «Чайни».
# Telegram Mini App отключён. Все страницы закрыты от встраивания через CSP,
# чтобы чужой сайт не мог наложить свой интерфейс поверх витрины или форм.

server {
    listen 80;
    listen [::]:80;
    server_name chainya.ru www.chainya.ru;

    location ^~ /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://chainya.ru$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name www.chainya.ru;

    ssl_certificate /etc/letsencrypt/live/chainya.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chainya.ru/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    return 301 https://chainya.ru$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name chainya.ru;

    ssl_certificate /etc/letsencrypt/live/chainya.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chainya.ru/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    root /var/www/chainya;
    index index.html;
    access_log /var/log/nginx/chainya.access.log;
    error_log /var/log/nginx/chainya.error.log;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Cross-Origin-Opener-Policy "same-origin" always;
    add_header X-Permitted-Cross-Domain-Policies "none" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;

    location ~ "^/api/admin/catalog/items/[a-z0-9][a-z0-9-]{0,79}/image$" {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        client_max_body_size 8m;
    }

    location /api/admin/ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Pragma "no-cache" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 32k;
    }

    # Mock-оплата никогда не публикуется на production-vhost, даже если старый
    # env по ошибке оставил backend в test mode.
    location ~ ^/api/orders/[^/]+/test-pay/?$ {
        access_log off;
        error_log /dev/null crit;
        return 404;
    }

    # Все операции с конкретным заказом могут нести bearer-token в query string.
    # Regex закрывает также trailing slash и будущие дочерние маршруты.
    location ~ ^/api/orders/ {
        access_log off;
        error_log /dev/null crit;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 32k;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 32k;
    }

    location ^~ /test-payment/ {
        access_log off;
        error_log /dev/null crit;
        return 404;
    }

    location ^~ /payment/ {
        access_log off;
        error_log /dev/null crit;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ~ ^/account/?$ {
        access_log off;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 32k;
    }

    location /admin/ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/catalog {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/catalog/ { return 308 /manage/catalog; }

    location = /manage/site {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/site/ { return 308 /manage/site; }

    location ~ ^/manage/(?:catalog|site)\.js$ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/guides {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/guides/ { return 308 /manage/guides; }

    location = /manage/promos {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /manage/promos/ { return 308 /manage/promos; }

    location ^~ /catalog-media/ {
        access_log off;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Индексируемые карточки товаров и sitemap строятся из живого каталога.
    # Поэтому изменения владельца в админ-панели видны поисковику без релиза.
    location ~ "^/((?:en/|zh/)?(?:tea|teaware)/[a-z0-9][a-z0-9-]{0,79})/$" {
        return 308 /$1;
    }

    location ~ "^/(?:en/|zh/)?(?:tea|teaware)/[a-z0-9][a-z0-9-]{0,79}$" {
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /sitemap.xml {
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /.well-known/security.txt {
        default_type text/plain;
        try_files /.well-known/security.txt =404;
    }

    # Не позволяем SPA-маршрутизации маскировать чувствительные файлы.
    location ~* "(^|/)(?:\.env(?:\..*)?|\.git(?:/|$)|\.DS_Store$|\._[^/]*$|__pycache__(?:/|$)|README(?:\.[^/]*)?$)" {
        return 404;
    }
    location ~* \.(?:py|pyc|zip|tar|gz|sql|sqlite|sqlite3|db|bak|backup|old)$ {
        return 404;
    }

    # HTML и основной JS обновляются одним release и проходят обязательную
    # перепроверку. Фото и шрифты ниже кэшируются отдельно.
    location = / {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-cache" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src https://yandex.ru; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        try_files /index.html =404;
    }

    location = /index.html {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-cache" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src https://yandex.ru; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        return 308 /;
    }

    location ~ ^/(en|zh)$ {
        return 308 /$1/;
    }

    location ~ ^/(en|zh)/$ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-cache" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src https://yandex.ru; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        try_files /$1/index.html =404;
    }

    error_page 404 /404.html;
    error_page 500 502 503 504 /50x.html;
    location = /404.html {
        internal;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    location = /50x.html {
        internal;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    # Публичные разделы имеют читаемые URL. Отдаём то же приложение только для
    # известного белого списка; произвольные пути по-прежнему честно получают 404.
    location ~ ^/(shop|business|booking)/$ {
        return 308 /$1;
    }

    location ~ ^/(shop|business|booking)$ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-cache" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src https://yandex.ru; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        try_files /$1/index.html =404;
    }

    location ~ ^/(en|zh)/(shop|teaware|business|booking)/$ {
        return 308 /$1/$2;
    }

    location ~ ^/(en|zh)/(shop|teaware|business|booking)$ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-cache" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src https://yandex.ru; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        try_files /$1/$2/index.html =404;
    }

    location ^~ /assets/ {
        try_files $uri =404;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "no-cache" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    location / { try_files $uri $uri/ =404; }

    location ~* \.(woff2?|ttf)$ {
        types {
            font/woff woff;
            font/woff2 woff2;
            font/ttf ttf;
        }
        expires 30d;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "public";
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    location ~* \.(jpg|jpeg|png|webp|svg|ico)$ {
        expires 7d;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Cross-Origin-Opener-Policy "same-origin" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Cache-Control "public";
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
}
