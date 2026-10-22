# GitHub Actions가 Google Cloud에 배포할 수 있게 하기 (Workload Identity Federation)

GitHub Actions가 서비스 계정 키 파일 없이 Google Cloud에 배포하게 하는 절차다. 사람과 코딩 에이전트가 모두 이 순서대로 진행한다.

- **키 파일(JSON)을 만들지 않는다.** `gcloud iam service-accounts keys create` 는 쓰지 않는다.
- **Owner, Editor 같은 넓은 역할을 주지 않는다.** 아래 표의 역할만 준다.
- 명령마다 무엇을 만들고 누구에게 어떤 권한을 주는지 확인하고 실행한다.
- 풀, 공급자, 권한 연결은 반영되는 데 최대 5분쯤 걸린다. 바로 실패하면 몇 분 뒤 다시 시도한다.

## 만드는 것과 주는 권한

| 대상 | 이름 | 받는 권한 | 범위 |
| --- | --- | --- | --- |
| 실행용 서비스 계정 | `friend-agent-runtime` | 비밀값 읽기 `roles/secretmanager.secretAccessor` | 비밀값 `llm-api-key` 하나 |
| 빌드용 서비스 계정 | `friend-agent-builder` | 소스 빌드 후 배포 `roles/run.builder` | 프로젝트 |
| 배포용 서비스 계정 | `github-deployer` | 소스 배포 `roles/run.sourceDeveloper` | 프로젝트 |
| | | API 사용 `roles/serviceusage.serviceUsageConsumer` | 프로젝트 |
| | | 서비스 계정 사용 `roles/iam.serviceAccountUser` | 실행용 서비스 계정 하나 |
| | | 빌드 계정 사용 `roles/iam.serviceAccountUser` | 빌드용 서비스 계정 하나 |
| | | (이미지 배포 때 추가) 이미지 올리기 `roles/artifactregistry.writer` | Artifact Registry 저장소 `friend-agent` 하나 |
| Workload Identity 풀 | `github` | | |
| 풀의 공급자 | `friend-agent-repo` | 내 저장소에서 온 요청만 받는 조건 | |
| 내 저장소의 워크플로 | | 배포용 서비스 계정으로 행동 `roles/iam.workloadIdentityUser` | 배포용 서비스 계정 하나 |

## 0. 값 정하기

```text
PROJECT_ID=<프로젝트 ID>
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
REPO=<GitHub 계정>/<저장소 이름>          # 예: jisu/friend-agent
```

## 1. 실행용, 빌드용 서비스 계정

실행용 계정은 에이전트 서비스가 실행될 때 쓰는 계정이다. 다른 서비스와 같이 쓰는 기본 서비스 계정 대신 이 서비스 전용 계정을 둔다.

빌드용 계정은 소스 배포에서 Cloud Build가 이미지를 만들고 배포할 때 쓰는 계정이다. 따로 정하지 않으면 Compute Engine 기본 서비스 계정으로 빌드하는데, 조직 없이 만든 프로젝트에서는 이 계정에 Editor가 붙어 있는 경우가 많다. 그러면 빌드를 시작할 수 있는 배포용 계정이 사실상 Editor 권한을 갖게 되므로, 빌드에 필요한 역할만 가진 계정을 따로 둔다.

```text
gcloud iam service-accounts create friend-agent-runtime --project $PROJECT_ID \
  --display-name "friend-agent 실행용"

gcloud secrets add-iam-policy-binding llm-api-key --project $PROJECT_ID \
  --member "serviceAccount:friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor

gcloud iam service-accounts create friend-agent-builder --project $PROJECT_ID \
  --display-name "friend-agent 빌드용"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member "serviceAccount:friend-agent-builder@$PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/run.builder
```

## 2. 배포용 서비스 계정과 권한

GitHub Actions가 배포할 때 쓰는 계정이다.

```text
gcloud iam service-accounts create github-deployer --project $PROJECT_ID \
  --display-name "GitHub Actions 배포용"

DEPLOYER=github-deployer@$PROJECT_ID.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/run.sourceDeveloper
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/serviceusage.serviceUsageConsumer

gcloud iam service-accounts add-iam-policy-binding \
  friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com --project $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser

# 배포용 계정이 빌드용 계정으로 빌드를 실행할 수 있게 한다.
gcloud iam service-accounts add-iam-policy-binding \
  friend-agent-builder@$PROJECT_ID.iam.gserviceaccount.com --project $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser
```

`roles/iam.serviceAccountUser` 는 프로젝트 전체가 아니라 실행용, 빌드용 서비스 계정 두 개에만 준다. 프로젝트 전체에 주거나 Compute Engine 기본 서비스 계정에 주면 배포용 계정이 넓은 권한을 가진 계정으로 행동할 수 있다. 빌드용 계정 권한이 없으면 배포 단계가 `Your account does not have permission to check details of build service account` 경고 뒤에 실패한다.

## 3. Workload Identity 풀과 공급자

```text
gcloud iam workload-identity-pools create github --project $PROJECT_ID \
  --location global --display-name "GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc friend-agent-repo --project $PROJECT_ID \
  --location global --workload-identity-pool github \
  --display-name "friend-agent 저장소" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition "assertion.repository=='$REPO'"
```

`--attribute-condition` 이 "내 저장소에서 온 요청만 받는다"는 조건이다. 이 조건이 없으면 GitHub의 다른 저장소도 이 공급자로 토큰을 받을 수 있다.

## 4. 내 저장소가 배포용 계정으로 행동하게 하기

```text
gcloud iam service-accounts add-iam-policy-binding $DEPLOYER --project $PROJECT_ID \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
```

## 5. (이미지 배포를 시작할 때) Artifact Registry 저장소와 쓰기 권한

```text
gcloud artifacts repositories create friend-agent --project $PROJECT_ID \
  --repository-format docker --location us-central1

gcloud artifacts repositories add-iam-policy-binding friend-agent --project $PROJECT_ID \
  --location us-central1 --member "serviceAccount:$DEPLOYER" --role roles/artifactregistry.writer
```

## 6. 기본 서비스 계정을 빌드에 쓰지 않기

빌드는 1단계의 빌드용 계정으로 한다. 배포용 계정에 Compute Engine 기본 서비스 계정을 쓸 권한(`roles/iam.serviceAccountUser`)을 주지 않는다. 이전 안내를 따라 이미 주었다면 아래 명령으로 회수한다.

```text
gcloud iam service-accounts remove-iam-policy-binding \
  $PROJECT_NUMBER-compute@developer.gserviceaccount.com --project $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser
```

## 7. GitHub 저장소에 값 넣기

워크플로가 읽는 값은 비밀값이 아니므로 Variables 에 넣는다.

```text
WIF_PROVIDER=$(gcloud iam workload-identity-pools providers describe friend-agent-repo --project $PROJECT_ID \
  --location global --workload-identity-pool github --format 'value(name)')

gh variable set GCP_PROJECT_ID --body "$PROJECT_ID"
gh variable set WIF_PROVIDER --body "$WIF_PROVIDER"
gh variable set DEPLOY_SA --body "$DEPLOYER"
gh variable set RUNTIME_SA --body "friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com"
gh variable set BUILD_SA --body "friend-agent-builder@$PROJECT_ID.iam.gserviceaccount.com"
```

화면으로 넣을 때: GitHub 저장소 > Settings > Secrets and variables > Actions > Variables 탭 > New repository variable.

## 8. 확인

- Google Cloud 콘솔 > IAM 및 관리자 > 워크로드 아이덴티티 제휴: 목록에는 표시 이름이 나온다. 풀 `GitHub Actions` 안에 공급자 `friend-agent 저장소` 가 보이고, 공급자의 수정(연필) 화면에서 속성 조건에 내 저장소 이름이 있다. 명령으로는 `gcloud iam workload-identity-pools providers describe friend-agent-repo --project $PROJECT_ID --location global --workload-identity-pool github --format "value(attributeCondition)"`.
- IAM 및 관리자 > 서비스 계정: `github-deployer`, `friend-agent-runtime`, `friend-agent-builder` 가 있고 키 탭에 키가 하나도 없다.
- 배포 후 Cloud Build > 기록: 최근 빌드의 서비스 계정이 `friend-agent-builder` 다.
- IAM 및 관리자 > IAM: `github-deployer` 의 역할이 위 표와 같다. Owner, Editor 가 없다.
