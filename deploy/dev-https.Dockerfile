FROM nginx:1.27-alpine

COPY deploy/dev-https.nginx.conf /etc/nginx/nginx.conf
