# Frontend: build the Vite/React bundle, then serve it with nginx (which also
# reverse-proxies /api to the backend container).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.30.5-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY nginx-main.conf /etc/nginx/nginx.conf
COPY api-proxy.conf /etc/nginx/api-proxy.conf
COPY access-policy.conf /etc/nginx/access-policy.conf
# COPY preserves host modes; the nginx runtime user must read these app assets.
RUN chmod -R a=rX /usr/share/nginx/html /etc/nginx/conf.d/default.conf \
    /etc/nginx/security-headers.conf /etc/nginx/nginx.conf \
    /etc/nginx/api-proxy.conf /etc/nginx/access-policy.conf
USER nginx
EXPOSE 8080
ENTRYPOINT []
CMD ["nginx", "-g", "daemon off;"]
