# Integração Google Drive — guia de desenvolvimento

## O que foi implementado

- OAuth Desktop por navegador, PKCE, callback em loopback e acesso offline com `drive.file`.
- Credenciais somente no Secret Service do Ubuntu, por identidade persistente da instalação; sem fallback em arquivo texto.
- Renovação de credenciais e mensagens de erro sem corpo de respostas, tokens ou URLs de sessão.
- Drive v3 com arquivos individuais e hierarquia de pastas. Snapshots de até 5 MiB usam multipart; maiores usam upload retomável por blocos de 8 MiB. Até quatro uploads independentes podem avançar em paralelo, com clientes HTTP próprios e SQLite no controlador.
- Identidade de objetos em `appProperties` com chaves derivadas por hash, sem colocar caminhos arbitrariamente longos em propriedades limitadas.
- IDs Drive pré-gerados, persistidos antes de criar, permitindo repetir operações após respostas perdidas sem criar outra identidade.
- Metadados JSON versão 1 na descrição dos arquivos: identificadores, caminho relativo original, permissões comuns, mtime_ns, estado e data de exclusão quando aplicável.
- Retenção em `<Destino>/<Computador>/Excluídos/<rootUUID>/<fileUUID>/<nome>` para evitar colisões entre caminhos e arquivos.
- Atualização de conteúdo, nome e pasta pai; itens excluídos remotamente não são recriados por retenção/limpeza. Uma nova edição local pode criar uma nova cópia.
- Limpeza utiliza `trashed=true`, nunca exclusão definitiva.
- Estado local vinculado a `about.user.permissionId`. Trocar a conta não reutiliza silenciosamente os IDs da conta anterior.
- Simulador permanece padrão; não converter automaticamente estado de simulação com arquivos observados para backend real.

O código tem testes com dublês da API e validação real do login, envio (incluindo múltiplos blocos), edição, renomeação, retenção, reaparecimento e pausa. A release pública atual é `0.1.1`, com correções de segurança da interface local. O mantenedor confirmou o fluxo manual de desinstalação, limpeza do estado de teste, nova autorização e reinstalação com backup em outro Ubuntu na `0.1.0`; recuperação, falhas de rede e escala continuam exigindo avaliação conforme o ambiente e o acervo.

## Estado da distribuição oficial

Atualização do mantenedor em 2 de novembro de 2026:

- Projeto OAuth oficial em produção; destinatários não precisam ser incluídos individualmente como testadores.
- Propriedade de `erservices.top` verificada no Google Search Console.
- Certificado HTTPS emitido pelo GitHub Pages e HTTPS obrigatório habilitado.
- Página inicial: https://files2gdrive.erservices.top/
- Política de Privacidade: https://files2gdrive.erservices.top/privacidade/
- Termos de Uso: https://files2gdrive.erservices.top/termos/
- Domínio autorizado no GCP: `erservices.top`.

Esse status não elimina políticas corporativas do Workspace, não define a estabilidade da API do aplicativo e não afirma aprovação de verificações de marca que não tenham sido confirmadas pelo Google. A preparação abaixo continua relevante para desenvolvimento e distribuições com cliente OAuth próprio.

## Preparação de um projeto próprio pelo mantenedor

1. Criar projeto Google Cloud de desenvolvimento e habilitar Google Drive API.
2. Configurar Google Auth Platform, público externo e dados de contato/consentimento.
3. Solicitar apenas `https://www.googleapis.com/auth/drive.file` inicialmente.
4. Criar cliente do tipo Desktop app e obter o JSON para o ambiente de desenvolvimento.
5. Se o projeto estiver em Testing, adicionar a conta de teste autorizada. Tokens para escopos Drive podem expirar em sete dias nesse estado.
6. Configurar o backend usando o JSON pelo comando `configure-drive`, seguido de `login`. Não enviar tokens ou senhas pelo chat e não versionar credenciais.
7. Antes da distribuição, preparar projeto de produção e publicação do OAuth, identidade pública, suporte, homepage e política de privacidade, validando requisitos atuais de verificação.

Mantenha o JSON OAuth local fora do repositório público. O construtor do pacote o recebe explicitamente para integrar a identificação do distribuidor; o código-fonte público não precisa conter esse arquivo. O cliente Desktop não é um segredo confiável em um aplicativo distribuído, mas isso não justifica publicar tokens. O JSON do cliente não é copiado para logs nem SQLite: a configuração local guarda somente seu caminho. Os tokens do usuário são armazenados no cofre.

## Roteiro de validação real (parcialmente concluído)

Usar pasta com dados de teste, antes de incluir um acervo importante, para validar:

- Login em sessão Ubuntu real com cofre desbloqueado; renovação e reautenticação.
- Upload/edição de arquivo oculto, arquivo vazio e arquivo maior que um bloco.
- Renomeação e movimentação, retenção e reaparecimento.
- Interrupção da rede no upload; repetição após falha e ausência de duplicatas.
- Execução após reiniciar processo e computador.
- Quota, acesso revogado e mensagens de erro sem credenciais.
- Conta diferente recusada para um estado já vinculado.
- Descoberta dos objetos do mesmo cliente OAuth em instalação nova — consultar `RESTAURACAO-E-RECONEXAO.md`.

## Limitações técnicas abertas

- Sessões resumable não são persistidas entre chamadas/reinícios. A repetição preserva a identidade, mas pode reenviar conteúdo inteiro.
- Metadados em descrição são remotos e versionados, mas podem ser alterados manualmente no Drive. A restauração deverá validar esquema, caminhos e permissões antes de aplicá-los.
- Diretórios vazios e permissões de diretórios ainda não são enviados.
- Renomear diretórios atualiza os caminhos dos arquivos por reconciliação; pastas remotas antigas vazias ainda podem permanecer.
- Durante transferência, o encerramento atual aguarda o trabalho em andamento. Cancelamento entre blocos ainda será integrado ao controle do motor.
- Não há exclusão definitiva automática. Interface web, bandeja e instalador estão documentados nos respectivos guias.
- O desenvolvimento usa faixas de versões; o empacotamento usa o lock com versões e hashes em `packaging/requirements-release.lock`.

## Referências oficiais consultadas

- [OAuth para aplicativos instalados](https://developers.google.com/identity/protocols/oauth2/native-app)
- [google-auth-oauthlib — InstalledAppFlow](https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html)
- [keyring — documentação](https://keyring.readthedocs.io/en/latest/)
- [Drive — escopos](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Drive — uploads e IDs pré-gerados](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [Drive — generateIds](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/generateIds)
- [Cliente Google Python — media e next_chunk](https://github.com/googleapis/google-api-python-client/blob/ce8b4331dddfa4026c2c97514720c4fb34650a18/docs/media.md)
- [Google — produção](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)
