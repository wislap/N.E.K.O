import clsx from 'clsx';
import type {
  ChatMessage,
  MessageAction,
  ToolActionRecommendationBlock as ToolActionRecommendationBlockModel,
} from './message-schema';

type ToolActionRecommendationBlockProps = {
  block: ToolActionRecommendationBlockModel;
  message: ChatMessage;
  onAction?: (message: ChatMessage, action: MessageAction) => void;
};

function directionLabel(direction: ToolActionRecommendationBlockModel['actions'][number]['direction']) {
  return direction === 'enable' ? '启用' : '关闭';
}

function statusLabel(status: NonNullable<ToolActionRecommendationBlockModel['status']>) {
  switch (status) {
    case 'applying':
      return '应用中';
    case 'applied':
      return '已应用';
    case 'dismissed':
      return '已略过';
    case 'failed':
      return '应用失败';
    case 'stale':
      return '已过期';
    case 'pending':
    default:
      return '';
  }
}

function emitRecommendationAction(
  message: ChatMessage,
  block: ToolActionRecommendationBlockModel,
  onAction: ToolActionRecommendationBlockProps['onAction'],
  action: MessageAction,
) {
  onAction?.(message, {
    ...action,
    payload: {
      requestId: block.requestId,
      recommendationId: block.recommendationId,
      slateId: block.slateId,
      ...(action.payload ?? {}),
    },
  });
}

export default function ToolActionRecommendationBlock({
  block,
  message,
  onAction,
}: ToolActionRecommendationBlockProps) {
  const status = block.status ?? 'pending';
  const isBusy = status === 'applying';
  const isClosed = status === 'applied' || status === 'dismissed' || status === 'stale';

  return (
    <section
      className="message-block tool-action-recommendation"
      data-recommendation-status={status}
      aria-label="工具开关建议"
    >
      <div className="tool-action-recommendation-summary">
        <span>{block.summary}</span>
        {status !== 'pending' ? (
          <span className="tool-action-recommendation-status">{statusLabel(status)}</span>
        ) : null}
      </div>

      <div className="tool-action-recommendation-list" role="list">
        {block.actions.length > 0 ? block.actions.map((toolAction, index) => {
          const positivePressed = toolAction.preferenceFeedback === 'positive';
          const negativePressed = toolAction.preferenceFeedback === 'negative';

          return (
            <div
              key={`${toolAction.toolId}-${toolAction.direction}-${index}`}
              className="tool-action-recommendation-row"
              role="listitem"
            >
              <div className="tool-action-recommendation-tool" title={toolAction.toolName}>
                {toolAction.toolName}
              </div>
              <div
                className={clsx(
                  'tool-action-recommendation-direction',
                  `is-${toolAction.direction}`,
                )}
              >
                {directionLabel(toolAction.direction)}
              </div>
              <div className="tool-action-recommendation-row-feedback" aria-label={`${toolAction.toolName} 偏好反馈`}>
                <button
                  type="button"
                  className={clsx('tool-action-recommendation-mini-button', {
                    'is-selected': positivePressed,
                  })}
                  aria-pressed={positivePressed}
                  disabled={isBusy || toolAction.disabled}
                  aria-label="赞这个工具建议"
                  onClick={() => emitRecommendationAction(message, block, onAction, {
                    id: 'tool-recommendation.action-feedback',
                    label: '赞',
                    action: 'tool-recommendation.action-feedback',
                    payload: {
                      toolId: toolAction.toolId,
                      direction: toolAction.direction,
                      value: 'positive',
                    },
                  })}
                >
                  👍
                </button>
                <button
                  type="button"
                  className={clsx('tool-action-recommendation-mini-button', {
                    'is-selected': negativePressed,
                  })}
                  aria-pressed={negativePressed}
                  disabled={isBusy || toolAction.disabled}
                  aria-label="踩这个工具建议"
                  onClick={() => emitRecommendationAction(message, block, onAction, {
                    id: 'tool-recommendation.action-feedback',
                    label: '踩',
                    action: 'tool-recommendation.action-feedback',
                    payload: {
                      toolId: toolAction.toolId,
                      direction: toolAction.direction,
                      value: 'negative',
                    },
                  })}
                >
                  👎
                </button>
              </div>
            </div>
          );
        }) : (
          <div className="tool-action-recommendation-empty" role="listitem">
            当前不建议调整工具开关
          </div>
        )}
      </div>

      <div className="tool-action-recommendation-footer">
        <button
          type="button"
          className="tool-action-recommendation-button is-primary"
          disabled={isBusy || isClosed || block.actions.length === 0}
          onClick={() => emitRecommendationAction(message, block, onAction, {
            id: 'tool-recommendation.apply',
            label: '应用建议',
            action: 'tool-recommendation.apply',
            variant: 'primary',
            payload: {
              actions: block.actions,
            },
          })}
        >
          应用建议
        </button>
        <button
          type="button"
          className="tool-action-recommendation-button"
          disabled={isBusy || isClosed}
          onClick={() => emitRecommendationAction(message, block, onAction, {
            id: 'tool-recommendation.dismiss',
            label: '暂不需要',
            action: 'tool-recommendation.dismiss',
            variant: 'secondary',
          })}
        >
          暂不需要
        </button>
        <button
          type="button"
          className={clsx('tool-action-recommendation-icon-button', {
            'is-selected': block.feedback === 'up',
          })}
          aria-label="这条建议有帮助"
          aria-pressed={block.feedback === 'up'}
          disabled={isBusy}
          onClick={() => emitRecommendationAction(message, block, onAction, {
            id: 'tool-recommendation.feedback',
            label: '赞',
            action: 'tool-recommendation.feedback',
            payload: {
              value: 'up',
            },
          })}
        >
          👍
        </button>
        <button
          type="button"
          className={clsx('tool-action-recommendation-icon-button', {
            'is-selected': block.feedback === 'down',
          })}
          aria-label="这条建议没有帮助"
          aria-pressed={block.feedback === 'down'}
          disabled={isBusy}
          onClick={() => emitRecommendationAction(message, block, onAction, {
            id: 'tool-recommendation.feedback',
            label: '踩',
            action: 'tool-recommendation.feedback',
            payload: {
              value: 'down',
            },
          })}
        >
          👎
        </button>
      </div>
    </section>
  );
}
