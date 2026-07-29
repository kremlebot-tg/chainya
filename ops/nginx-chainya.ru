# chainya.ru — сайт и checkout «Чайни».
# X-Frame-Options и CSP frame-ancestors намеренно не задаются: сайт работает
# как Telegram Mini App. Остальные заголовки не мешают встраиванию.

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
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
    add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;

    location /api/admin/ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "no-store" always;
        add_header Pragma "no-cache" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
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

    location ^~ /test-payment/ { return 404; }

    location /payment/ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /admin/ {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
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
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
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
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        proxy_pass http://127.0.0.1:8077;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
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

    # HTML содержит весь JavaScript приложения, поэтому его нельзя оставлять в
    # браузере после релиза. Фото и шрифты ниже по-прежнему кэшируются отдельно.
    location = / {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "no-cache, no-store, must-revalidate" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
        expires -1;
        try_files /index.html =404;
    }

    location = /index.html {
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "no-cache, no-store, must-revalidate" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
        expires -1;
    }

    error_page 404 /404.html;
    location = /404.html {
        internal;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    # Клиентские экраны используют hash-маршруты (#shop, #book), поэтому
    # несуществующие URL-пути не должны получать главную страницу.
    location / { try_files $uri $uri/ =404; }

    location ~* \.(woff2?|ttf)$ {
        expires 30d;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "public";
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    location ~* \.(jpg|jpeg|png|webp|svg|ico)$ {
        expires 7d;
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Cache-Control "public";
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
        add_header Content-Security-Policy "base-uri 'self'; object-src 'none'; form-action 'self'" always;
    }

    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
}
