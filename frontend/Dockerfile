FROM node:22-alpine AS build
WORKDIR /w
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /w/dist /usr/share/nginx/html
EXPOSE 8080
