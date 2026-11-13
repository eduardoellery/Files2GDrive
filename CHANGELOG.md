# Histórico de versões

O projeto usa [versionamento semântico](https://semver.org/lang/pt-BR/), com tags `vMAJOR.MINOR.PATCH`. A série `0.x` representa a evolução inicial e não promete uma API estável. Os artefatos de uma versão publicada não são substituídos por conteúdos diferentes.

## 0.1.1 — 2026-11-13

Versão de correção de segurança para Ubuntu 24.04 LTS amd64.

- Remove o token de abertura da linha de comando do navegador; o bootstrap usa arquivo privado 0600, POST de uso único e validade de 60 segundos.
- Revoga a sessão ligada a um token se o token consumido for reapresentado.
- Substitui autenticação por cookie por bearer por sessão de navegador, enviado no cabeçalho `Authorization` em todas as rotas `/api/` e expirado após 12 horas sem atividade.
- Remove o cookie legado durante o bootstrap e mantém proteção de origem e CSRF nas alterações.

## 0.1.0 — 2026-11-02

Primeira release pública sem sufixo de pré-lançamento, após validação do fluxo de instalação limpa, nova autorização Google e backup em outro Ubuntu.

- Instalador `files2gdrive_0.1.0_amd64.deb` para Ubuntu 24.04 LTS amd64, com dependências Python incluídas.
- Backup contínuo Ubuntu → Google Drive, seletor de pastas e diretórios ocultos, exceções por subpasta e filtros com coringas.
- Retenção de arquivos removidos por 30 dias em Excluídos, seguida de envio à lixeira do Drive.
- Fila persistente, snapshots privados, até quatro uploads simultâneos e preparação de diretórios em lotes.
- Interface web local, controles na bandeja, indicador de autenticação, logs, restauração e reconexão.
- OAuth oficial em produção e site com domínio verificado e HTTPS obrigatório, política de privacidade e termos públicos.
- Versão e nomes de downloads padronizados; atualização a partir de `0.1.0~alpha3` preserva o perfil. Esquema do banco permanece em 4.

As limitações documentadas continuam aplicáveis: sem suporte validado ao Ubuntu 26.04 ou outras arquiteturas; pastas vazias, links simbólicos e arquivos especiais não são enviados. A publicação não adiciona novos comportamentos ao motor de backup da pré-release anterior.

## 0.1.0-alpha3 — pré-release anterior

Versão de teste anterior à release pública. Continua disponível no histórico do GitHub com seus artefatos originais. A versão interna do pacote Debian é `0.1.0~alpha3`.
