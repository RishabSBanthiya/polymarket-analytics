"""
ML Correlation Model for Sports Markets.

Predicts correlations between markets within a game using:
1. Structural features (market types, teams, players)
2. Historical features (past game data)
3. Price features (volatility, liquidity)

Supports multiple model types:
- Gradient Boosting (default)
- Random Forest
- Neural Network
"""

import logging
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
import numpy as np

from .models import (
    SportsGame,
    GameMarket,
    MarketType,
    CorrelationMatrix,
    MLFeatures,
    HistoricalGameData,
)
from .config import MLModelConfig

logger = logging.getLogger(__name__)

# Optional ML imports
try:
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.model_selection import cross_val_score, train_test_split
    from sklearn.preprocessing import StandardScaler
    from sklearn.calibration import CalibratedClassifierCV
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed. ML features disabled.")

try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    torch = None
    nn = None


# Only define neural network if PyTorch is available
if TORCH_AVAILABLE:
    class CorrelationNN(nn.Module):
        """Neural network for correlation prediction."""

        def __init__(self, input_dim: int = 12, hidden_dims: List[int] = None):
            super().__init__()
            hidden_dims = hidden_dims or [64, 32, 16]

            layers = []
            prev_dim = input_dim
            for hidden_dim in hidden_dims:
                layers.extend([
                    nn.Linear(prev_dim, hidden_dim),
                    nn.ReLU(),
                    nn.Dropout(0.2),
                ])
                prev_dim = hidden_dim

            # Output: correlation (-1 to 1) and confidence (0 to 1)
            layers.append(nn.Linear(prev_dim, 2))

            self.network = nn.Sequential(*layers)

        def forward(self, x):
            out = self.network(x)
            # Apply tanh to correlation, sigmoid to confidence
            correlation = torch.tanh(out[:, 0])
            confidence = torch.sigmoid(out[:, 1])
            return torch.stack([correlation, confidence], dim=1)
else:
    CorrelationNN = None


class MLCorrelationModel:
    """
    ML model for predicting market correlations.

    Combines structural knowledge with learned patterns
    from historical game data.
    """

    def __init__(
        self,
        config: MLModelConfig,
        model_path: Optional[Path] = None,
    ):
        self.config = config
        self.model_path = model_path or Path("data/sports_correlation_model.pkl")

        # Model components
        self._model = None
        self._scaler = None
        self._is_trained = False

        # Training data
        self._training_data: List[Tuple[MLFeatures, float]] = []

        # Load existing model if available
        self._load_model()

    def predict_correlation_matrix(
        self,
        game: SportsGame,
    ) -> CorrelationMatrix:
        """
        Predict full correlation matrix for a game's markets.

        Uses ML model if trained, otherwise falls back to structural.
        """
        markets = game.markets
        n = len(markets)
        market_ids = [m.token_id for m in markets]

        correlation = np.eye(n)  # Diagonal is 1
        confidence = np.eye(n)   # High confidence on diagonal

        for i in range(n):
            for j in range(i + 1, n):
                corr, conf = self.predict_correlation(
                    markets[i],
                    markets[j],
                    game,
                )
                correlation[i, j] = corr
                correlation[j, i] = corr
                confidence[i, j] = conf
                confidence[j, i] = conf

        model_type = "ml_predicted" if self._is_trained else "structural"

        return CorrelationMatrix(
            game_id=game.game_id,
            market_ids=market_ids,
            correlation=correlation,
            confidence=confidence,
            model_type=model_type,
        )

    def predict_correlation(
        self,
        market_a: GameMarket,
        market_b: GameMarket,
        game: SportsGame,
    ) -> Tuple[float, float]:
        """
        Predict correlation between two markets.

        Returns: (correlation, confidence)
        """
        # Extract features
        features = self._extract_features(market_a, market_b, game)

        # If model is trained, use it
        if self._is_trained and self._model is not None:
            try:
                return self._predict_with_model(features)
            except Exception as e:
                logger.warning(f"ML prediction failed, using structural: {e}")

        # Fall back to structural correlation
        return self._structural_correlation(market_a, market_b, game)

    def add_training_sample(
        self,
        market_a: GameMarket,
        market_b: GameMarket,
        game: SportsGame,
        realized_correlation: float,
    ) -> None:
        """Add a training sample from resolved game."""
        features = self._extract_features(market_a, market_b, game)
        self._training_data.append((features, realized_correlation))

    def train(self, historical_data: Optional[List[HistoricalGameData]] = None) -> Dict[str, float]:
        """
        Train the correlation model.

        Returns: Training metrics
        """
        if not SKLEARN_AVAILABLE:
            logger.error("scikit-learn required for training")
            return {"error": "sklearn not available"}

        # Combine with historical data if provided
        if historical_data:
            self._add_historical_data(historical_data)

        if len(self._training_data) < self.config.min_training_samples:
            logger.warning(
                f"Insufficient training data: {len(self._training_data)} < "
                f"{self.config.min_training_samples}"
            )
            return {"error": "insufficient_data", "samples": len(self._training_data)}

        # Prepare data
        X = np.array([f.to_array() for f, _ in self._training_data])
        y = np.array([corr for _, corr in self._training_data])

        # Scale features
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        # Split data
        X_train, X_val, y_train, y_val = train_test_split(
            X_scaled, y,
            test_size=self.config.validation_split,
            random_state=42,
        )

        # Train model based on type
        if self.config.model_type == "gradient_boosting":
            self._model = GradientBoostingRegressor(
                n_estimators=self.config.n_estimators,
                max_depth=self.config.max_depth,
                learning_rate=self.config.learning_rate,
                min_samples_split=self.config.min_samples_split,
                random_state=42,
            )
        elif self.config.model_type == "random_forest":
            self._model = RandomForestRegressor(
                n_estimators=self.config.n_estimators,
                max_depth=self.config.max_depth,
                min_samples_split=self.config.min_samples_split,
                random_state=42,
            )
        elif self.config.model_type == "neural_network" and TORCH_AVAILABLE:
            return self._train_neural_network(X_scaled, y)
        else:
            self._model = GradientBoostingRegressor(random_state=42)

        # Train
        self._model.fit(X_train, y_train)

        # Evaluate
        train_score = self._model.score(X_train, y_train)
        val_score = self._model.score(X_val, y_val)

        # Cross-validation
        cv_scores = None
        if self.config.use_cross_validation:
            cv_scores = cross_val_score(self._model, X_scaled, y, cv=self.config.cv_folds)

        self._is_trained = True
        self._save_model()

        metrics = {
            "train_r2": train_score,
            "val_r2": val_score,
            "samples": len(self._training_data),
        }
        if cv_scores is not None:
            metrics["cv_mean"] = float(np.mean(cv_scores))
            metrics["cv_std"] = float(np.std(cv_scores))

        logger.info(f"Model trained: {metrics}")
        return metrics

    def _train_neural_network(
        self,
        X: np.ndarray,
        y: np.ndarray,
    ) -> Dict[str, float]:
        """Train neural network model."""
        if not TORCH_AVAILABLE:
            logger.error("PyTorch required for neural network")
            return {"error": "torch not available"}

        # Convert to tensors
        X_tensor = torch.FloatTensor(X)
        y_tensor = torch.FloatTensor(y)

        # Create model
        model = CorrelationNN(input_dim=X.shape[1])
        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        criterion = nn.MSELoss()

        # Training loop
        epochs = 100
        batch_size = 32
        best_loss = float('inf')

        for epoch in range(epochs):
            model.train()
            total_loss = 0

            # Mini-batches
            indices = torch.randperm(len(X_tensor))
            for i in range(0, len(indices), batch_size):
                batch_idx = indices[i:i + batch_size]
                X_batch = X_tensor[batch_idx]
                y_batch = y_tensor[batch_idx]

                optimizer.zero_grad()
                outputs = model(X_batch)
                loss = criterion(outputs[:, 0], y_batch)
                loss.backward()
                optimizer.step()

                total_loss += loss.item()

            avg_loss = total_loss / (len(indices) // batch_size + 1)
            if avg_loss < best_loss:
                best_loss = avg_loss

        self._model = model
        self._is_trained = True

        return {"train_loss": best_loss, "epochs": epochs}

    def _predict_with_model(self, features: MLFeatures) -> Tuple[float, float]:
        """Make prediction using trained model."""
        X = features.to_array().reshape(1, -1)

        if self._scaler is not None:
            X = self._scaler.transform(X)

        if TORCH_AVAILABLE and isinstance(self._model, CorrelationNN):
            self._model.eval()
            with torch.no_grad():
                X_tensor = torch.FloatTensor(X)
                output = self._model(X_tensor)
                correlation = float(output[0, 0])
                confidence = float(output[0, 1])
        else:
            correlation = float(self._model.predict(X)[0])
            # Estimate confidence from model (simplified)
            confidence = min(0.9, 0.5 + 0.1 * len(self._training_data) / 1000)

        # Clamp values
        correlation = max(-1.0, min(1.0, correlation))
        confidence = max(0.0, min(1.0, confidence))

        return correlation, confidence

    def _extract_features(
        self,
        market_a: GameMarket,
        market_b: GameMarket,
        game: SportsGame,
    ) -> MLFeatures:
        """Extract features for model input."""
        # Structural correlation as a feature
        struct_corr, _ = self._structural_correlation(market_a, market_b, game)

        return MLFeatures(
            market_a_type=market_a.market_type.value,
            market_b_type=market_b.market_type.value,
            same_team=(market_a.team is not None and market_a.team == market_b.team),
            same_player=(market_a.player is not None and market_a.player == market_b.player),
            structural_correlation=struct_corr,
            sport=game.sport.value,
            is_playoff=self._is_playoff_game(game),
            spread_vegas=None,  # Would need external data
            total_vegas=None,
            historical_correlation=None,  # Could be populated from cache
            price_volatility_a=self._estimate_volatility(market_a),
            price_volatility_b=self._estimate_volatility(market_b),
        )

    def _structural_correlation(
        self,
        market_a: GameMarket,
        market_b: GameMarket,
        game: SportsGame,
    ) -> Tuple[float, float]:
        """
        Calculate structural (logical) correlation.

        Based on known relationships between market types.
        Returns stronger negative correlations for hedgeable pairs.
        """
        # Winner markets for different teams: perfect negative
        if (
            market_a.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
            and market_b.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
            and market_a.team and market_b.team
            and market_a.team != market_b.team
        ):
            return -1.0, 0.95

        # Same market, YES vs NO outcomes: perfect negative
        if market_a.market_id == market_b.market_id:
            outcomes = {market_a.outcome.lower(), market_b.outcome.lower()}
            if outcomes == {"yes", "no"}:
                return -1.0, 1.0

        # Spread markets for opposite teams: strong negative
        if (
            market_a.market_type == MarketType.SPREAD
            and market_b.market_type == MarketType.SPREAD
            and market_a.team and market_b.team
            and market_a.team != market_b.team
        ):
            return -0.85, 0.9

        # Winner vs opponent's player prop: moderate negative
        # (if team A wins, team B's star is less likely to dominate)
        if (
            market_a.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
            and market_b.market_type == MarketType.PLAYER_PROP
            and market_a.team and market_b.team
            and market_a.team != market_b.team
        ):
            return -0.4, 0.65

        # Player prop vs opponent's player prop: moderate negative (zero-sum game)
        if (
            market_a.market_type == MarketType.PLAYER_PROP
            and market_b.market_type == MarketType.PLAYER_PROP
            and market_a.team and market_b.team
            and market_a.team != market_b.team
        ):
            return -0.35, 0.6

        # Player props for same team: moderate negative (compete for opportunities)
        if (
            market_a.market_type == MarketType.PLAYER_PROP
            and market_b.market_type == MarketType.PLAYER_PROP
            and market_a.team == market_b.team
            and market_a.player != market_b.player
        ):
            return -0.30, 0.7

        # Total over/under: depends on which side
        if market_a.market_type == MarketType.TOTAL and market_b.market_type == MarketType.TOTAL:
            if market_a.threshold == market_b.threshold:
                # Same line, different sides
                return -1.0, 0.95

        # Winner and total under: negative (if favorite wins, likely lower scoring)
        if (
            market_a.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
            and market_b.market_type == MarketType.TOTAL
        ):
            question_lower = market_b.question.lower()
            if "under" in question_lower:
                return -0.3, 0.55
            if "over" in question_lower:
                return 0.2, 0.5

        # Note: Removed spread/total correlation rule as it creates invalid
        # covariance matrices when combined with other negative correlations

        # Winner and their player scoring high: positive
        if (
            market_a.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
            and market_b.market_type == MarketType.PLAYER_PROP
            and market_a.team == market_b.team
        ):
            return 0.45, 0.7

        # Default: assume weak correlation
        return 0.0, 0.4

    def _is_playoff_game(self, game: SportsGame) -> bool:
        """Check if game is a playoff game."""
        # Simple heuristic based on date and slug
        if game.slug:
            slug_lower = game.slug.lower()
            if any(x in slug_lower for x in ["playoff", "final", "championship", "series"]):
                return True
        return False

    def _estimate_volatility(self, market: GameMarket) -> float:
        """Estimate market volatility from spread and liquidity."""
        if market.spread:
            # Higher spread = more volatile/uncertain
            return min(1.0, market.spread / 10.0)
        return 0.5  # Default medium volatility

    def _add_historical_data(self, data: List[HistoricalGameData]) -> None:
        """Add historical game data to training set."""
        for game_data in data:
            # Compute realized correlations from resolutions
            resolutions = game_data.market_resolutions
            if len(resolutions) < 2:
                continue

            token_ids = list(resolutions.keys())
            for i, tid_a in enumerate(token_ids):
                for tid_b in token_ids[i + 1:]:
                    # Realized correlation: both resolve same way (+1) or opposite (-1)
                    res_a = 1.0 if resolutions[tid_a] else 0.0
                    res_b = 1.0 if resolutions[tid_b] else 0.0

                    if res_a == res_b:
                        realized = 1.0
                    else:
                        realized = -1.0

                    # Create dummy features (would need full market data in practice)
                    features = MLFeatures(
                        market_a_type="unknown",
                        market_b_type="unknown",
                        same_team=False,
                        same_player=False,
                        structural_correlation=0.0,
                        sport=game_data.sport.value,
                        is_playoff=False,
                        spread_vegas=None,
                        total_vegas=None,
                        historical_correlation=None,
                        price_volatility_a=0.5,
                        price_volatility_b=0.5,
                    )
                    self._training_data.append((features, realized))

    def _save_model(self) -> None:
        """Save trained model to disk."""
        if not self._is_trained:
            return

        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.model_path, 'wb') as f:
                pickle.dump({
                    'model': self._model,
                    'scaler': self._scaler,
                    'config': self.config,
                    'trained_at': datetime.now(timezone.utc).isoformat(),
                    'samples': len(self._training_data),
                }, f)
            logger.info(f"Model saved to {self.model_path}")
        except Exception as e:
            logger.warning(f"Failed to save model: {e}")

    def _load_model(self) -> None:
        """Load trained model from disk."""
        if not self.model_path.exists():
            return

        try:
            with open(self.model_path, 'rb') as f:
                data = pickle.load(f)
                self._model = data['model']
                self._scaler = data.get('scaler')
                self._is_trained = True
                logger.info(f"Loaded model from {self.model_path}")
        except Exception as e:
            logger.warning(f"Failed to load model: {e}")

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    @property
    def training_samples(self) -> int:
        return len(self._training_data)
