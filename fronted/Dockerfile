# fe/Dockerfile
FROM nginx:alpine
# 放自定义配置
COPY nginx.conf /etc/nginx/nginx.conf
# 放前端静态文件
COPY index.html /usr/share/nginx/html/index.html
# 如有其他资源（js/css），一并 COPY 进去：
# COPY assets/ /usr/share/nginx/html/assets/
