export const EMPTY_LLM_FORM = {
  provider_id: "",
  service_name: "",
  api_type: "anthropic",
  upstream_url: "",
  upstream_api_key: "",
  upstream_auth_type: "x-api-key",
  default_model: "",
  cheap_model: "",
  models: "",
  max_output_tokens: 65536,
};

export function llmConfigToForm(config) {
  if (!config) return { ...EMPTY_LLM_FORM };
  return {
    provider_id: config.provider_id,
    service_name: config.service_name,
    api_type: config.api_type,
    upstream_url: config.upstream_url,
    upstream_api_key: "",
    upstream_auth_type: config.upstream_auth_type,
    default_model: config.default_model,
    cheap_model: config.cheap_model,
    models: config.models.join(", "),
    max_output_tokens: config.max_output_tokens,
  };
}

export function llmFormToPayload(form) {
  const payload = {
    ...form,
    models: form.models.split(",").map((item) => item.trim()).filter(Boolean),
    max_output_tokens: Number(form.max_output_tokens),
  };
  if (!form.upstream_api_key) delete payload.upstream_api_key;
  return payload;
}
