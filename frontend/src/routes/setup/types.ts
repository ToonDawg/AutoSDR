import type { ConnectorType } from '@/lib/types';

/**
 * Shape of the three-step wizard form.
 *
 * The top-level :func:`Setup` coordinator owns a single `FormState` and
 * passes each step only the slice it mutates. Lifting state to the
 * coordinator keeps navigation simple (values persist when the user
 * clicks Back) and keeps derived helpers like `canAdvanceFromStep*`
 * in one place.
 */
export type Step = 0 | 1 | 2;

export interface FormState {
  business_name: string;
  business_dump: string;
  tone_prompt: string;

  llm_api_key: string;
  model_main: string;

  connector_type: ConnectorType;
  textbee_api_url: string;
  textbee_api_key: string;
  textbee_device_id: string;
  smsgate_api_url: string;
  smsgate_username: string;
  smsgate_password: string;
}

export const INITIAL_STATE: FormState = {
  business_name: '',
  business_dump: '',
  tone_prompt: '',
  llm_api_key: '',
  model_main: 'gemini/gemini-3-flash-preview',
  connector_type: 'file',
  textbee_api_url: 'https://api.textbee.dev',
  textbee_api_key: '',
  textbee_device_id: '',
  smsgate_api_url: 'http://localhost:3000/api/3rdparty/v1',
  smsgate_username: '',
  smsgate_password: '',
};

export type SetField = <K extends keyof FormState>(key: K, value: FormState[K]) => void;
