"""
Sport-Specific Model Trainer.

Trains separate ML correlation models for each sport,
using historical resolved market data.
"""

import logging
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import numpy as np

from .models import Sport, MarketType, MLFeatures
from .config import MLModelConfig
from .data_collector import SportsDataCollector

logger = logging.getLogger(__name__)

# Optional imports
try:
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.model_selection import cross_val_score, train_test_split, GridSearchCV
    from sklearn.preprocessing import StandardScaler, LabelEncoder
    from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed. Training disabled.")


class SportSpecificTrainer:
    """
    Trains sport-specific correlation models.

    Each sport gets its own model since correlation patterns
    differ significantly (e.g., NBA vs NFL scoring dynamics).
    """

    def __init__(
        self,
        config: MLModelConfig,
        models_dir: Path = None,
    ):
        self.config = config
        self.models_dir = models_dir or Path("data/sports_models")
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # Trained models per sport
        self._models: Dict[str, any] = {}
        self._scalers: Dict[str, StandardScaler] = {}
        self._encoders: Dict[str, Dict[str, LabelEncoder]] = {}
        self._metrics: Dict[str, Dict] = {}

    def train_all_sports(
        self,
        training_data: List[Dict],
        min_samples: int = 50,
    ) -> Dict[str, Dict]:
        """
        Train models for all sports with sufficient data.

        Returns: Metrics for each sport
        """
        if not SKLEARN_AVAILABLE:
            logger.error("scikit-learn required for training")
            return {}

        # Group by sport
        by_sport: Dict[str, List[Dict]] = {}
        for sample in training_data:
            sport = sample.get("sport", "unknown")
            if sport not in by_sport:
                by_sport[sport] = []
            by_sport[sport].append(sample)

        results = {}

        for sport, samples in by_sport.items():
            if len(samples) < min_samples:
                logger.info(f"Skipping {sport}: only {len(samples)} samples (need {min_samples})")
                continue

            logger.info(f"Training model for {sport} with {len(samples)} samples")

            try:
                metrics = self.train_sport(sport, samples)
                results[sport] = metrics
                self._metrics[sport] = metrics
            except Exception as e:
                logger.error(f"Failed to train {sport}: {e}", exc_info=True)
                results[sport] = {"error": str(e)}

        # Save all models
        self._save_models()

        return results

    def train_sport(
        self,
        sport: str,
        samples: List[Dict],
    ) -> Dict:
        """
        Train model for a specific sport.

        Returns: Training metrics
        """
        # Prepare features and targets
        X, y, feature_names = self._prepare_data(samples, sport)

        if len(X) < self.config.min_training_samples:
            return {"error": f"Insufficient data: {len(X)} samples"}

        # Split data
        X_train, X_test, y_train, y_test = train_test_split(
            X, y,
            test_size=self.config.validation_split,
            random_state=42,
        )

        # Scale features
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        # Train model with hyperparameter tuning
        if self.config.model_type == "gradient_boosting":
            model, best_params = self._train_gradient_boosting(X_train_scaled, y_train)
        elif self.config.model_type == "random_forest":
            model, best_params = self._train_random_forest(X_train_scaled, y_train)
        else:
            model, best_params = self._train_gradient_boosting(X_train_scaled, y_train)

        # Evaluate
        y_pred_train = model.predict(X_train_scaled)
        y_pred_test = model.predict(X_test_scaled)

        metrics = {
            "sport": sport,
            "samples": len(X),
            "train_r2": r2_score(y_train, y_pred_train),
            "test_r2": r2_score(y_test, y_pred_test),
            "train_mae": mean_absolute_error(y_train, y_pred_train),
            "test_mae": mean_absolute_error(y_test, y_pred_test),
            "train_rmse": np.sqrt(mean_squared_error(y_train, y_pred_train)),
            "test_rmse": np.sqrt(mean_squared_error(y_test, y_pred_test)),
            "best_params": best_params,
            "feature_names": feature_names,
        }

        # Cross-validation
        if self.config.use_cross_validation:
            cv_scores = cross_val_score(
                model, X_train_scaled, y_train,
                cv=self.config.cv_folds,
                scoring="r2",
            )
            metrics["cv_mean"] = float(np.mean(cv_scores))
            metrics["cv_std"] = float(np.std(cv_scores))

        # Feature importance
        if hasattr(model, "feature_importances_"):
            importance = dict(zip(feature_names, model.feature_importances_))
            metrics["feature_importance"] = dict(
                sorted(importance.items(), key=lambda x: -x[1])[:10]
            )

        # Store model
        self._models[sport] = model
        self._scalers[sport] = scaler

        logger.info(
            f"{sport} model trained: R2={metrics['test_r2']:.3f}, "
            f"MAE={metrics['test_mae']:.3f}"
        )

        return metrics

    def _prepare_data(
        self,
        samples: List[Dict],
        sport: str,
    ) -> Tuple[np.ndarray, np.ndarray, List[str]]:
        """Prepare feature matrix and target vector."""
        features = []
        targets = []

        # Encoders for categorical features
        if sport not in self._encoders:
            self._encoders[sport] = {
                "market_type_a": LabelEncoder(),
                "market_type_b": LabelEncoder(),
            }

        # Collect all market types for fitting encoders
        all_types_a = [s.get("market_type_a", "unknown") for s in samples]
        all_types_b = [s.get("market_type_b", "unknown") for s in samples]

        self._encoders[sport]["market_type_a"].fit(all_types_a + ["unknown"])
        self._encoders[sport]["market_type_b"].fit(all_types_b + ["unknown"])

        for sample in samples:
            # Extract features
            try:
                type_a = sample.get("market_type_a", "unknown")
                type_b = sample.get("market_type_b", "unknown")

                type_a_enc = self._encoders[sport]["market_type_a"].transform([type_a])[0]
                type_b_enc = self._encoders[sport]["market_type_b"].transform([type_b])[0]

                feat = [
                    type_a_enc,                                    # Market type A (encoded)
                    type_b_enc,                                    # Market type B (encoded)
                    1 if sample.get("same_team") else 0,           # Same team flag
                    1 if sample.get("same_player") else 0,         # Same player flag
                    self._structural_prior(type_a, type_b),        # Structural prior
                    self._type_interaction(type_a, type_b),        # Type interaction
                ]

                features.append(feat)
                targets.append(sample.get("realized_correlation", 0.0))

            except Exception as e:
                logger.debug(f"Failed to process sample: {e}")
                continue

        feature_names = [
            "market_type_a",
            "market_type_b",
            "same_team",
            "same_player",
            "structural_prior",
            "type_interaction",
        ]

        return np.array(features), np.array(targets), feature_names

    def _structural_prior(self, type_a: str, type_b: str) -> float:
        """Get structural correlation prior based on market types."""
        # Known structural relationships
        priors = {
            ("winner", "winner"): -0.8,      # Different team winners: negative
            ("winner", "player_prop"): 0.3,  # Team winning helps player
            ("player_prop", "player_prop"): -0.3,  # Players compete for stats
            ("total", "total"): -0.9,        # Over vs under
            ("winner", "total"): 0.1,        # Weak relationship
            ("spread", "winner"): 0.7,       # Spread aligned with winner
        }

        key = tuple(sorted([type_a, type_b]))
        return priors.get(key, 0.0)

    def _type_interaction(self, type_a: str, type_b: str) -> float:
        """Compute interaction feature between types."""
        same_type = 1.0 if type_a == type_b else 0.0
        return same_type

    def _train_gradient_boosting(
        self,
        X: np.ndarray,
        y: np.ndarray,
    ) -> Tuple[any, Dict]:
        """Train gradient boosting with grid search."""
        param_grid = {
            "n_estimators": [50, 100, 200],
            "max_depth": [3, 5, 7],
            "learning_rate": [0.05, 0.1, 0.2],
            "min_samples_split": [5, 10, 20],
        }

        base_model = GradientBoostingRegressor(random_state=42)

        # Simplified grid search if dataset is small
        if len(X) < 200:
            param_grid = {
                "n_estimators": [50, 100],
                "max_depth": [3, 5],
                "learning_rate": [0.1],
            }

        grid_search = GridSearchCV(
            base_model,
            param_grid,
            cv=min(3, len(X) // 20),
            scoring="r2",
            n_jobs=-1,
        )

        grid_search.fit(X, y)

        return grid_search.best_estimator_, grid_search.best_params_

    def _train_random_forest(
        self,
        X: np.ndarray,
        y: np.ndarray,
    ) -> Tuple[any, Dict]:
        """Train random forest with grid search."""
        param_grid = {
            "n_estimators": [50, 100, 200],
            "max_depth": [5, 10, 15],
            "min_samples_split": [5, 10],
        }

        base_model = RandomForestRegressor(random_state=42)

        grid_search = GridSearchCV(
            base_model,
            param_grid,
            cv=min(3, len(X) // 20),
            scoring="r2",
            n_jobs=-1,
        )

        grid_search.fit(X, y)

        return grid_search.best_estimator_, grid_search.best_params_

    def predict(
        self,
        sport: str,
        market_type_a: str,
        market_type_b: str,
        same_team: bool,
        same_player: bool,
    ) -> Tuple[float, float]:
        """
        Predict correlation for a market pair.

        Returns: (correlation, confidence)
        """
        if sport not in self._models:
            # Fall back to structural prior
            prior = self._structural_prior(market_type_a, market_type_b)
            return prior, 0.3

        model = self._models[sport]
        scaler = self._scalers[sport]
        encoders = self._encoders[sport]

        try:
            type_a_enc = encoders["market_type_a"].transform([market_type_a])[0]
            type_b_enc = encoders["market_type_b"].transform([market_type_b])[0]
        except:
            type_a_enc = 0
            type_b_enc = 0

        features = np.array([[
            type_a_enc,
            type_b_enc,
            1 if same_team else 0,
            1 if same_player else 0,
            self._structural_prior(market_type_a, market_type_b),
            self._type_interaction(market_type_a, market_type_b),
        ]])

        features_scaled = scaler.transform(features)
        prediction = model.predict(features_scaled)[0]

        # Clamp to valid range
        prediction = max(-1.0, min(1.0, prediction))

        # Confidence based on model performance
        sport_metrics = self._metrics.get(sport, {})
        confidence = min(0.9, max(0.4, sport_metrics.get("test_r2", 0.5)))

        return prediction, confidence

    def _save_models(self) -> None:
        """Save all trained models to disk."""
        for sport, model in self._models.items():
            model_path = self.models_dir / f"{sport}_correlation_model.pkl"

            try:
                with open(model_path, "wb") as f:
                    pickle.dump({
                        "model": model,
                        "scaler": self._scalers.get(sport),
                        "encoders": self._encoders.get(sport),
                        "metrics": self._metrics.get(sport),
                        "trained_at": datetime.now(timezone.utc).isoformat(),
                    }, f)
                logger.info(f"Saved {sport} model to {model_path}")
            except Exception as e:
                logger.warning(f"Failed to save {sport} model: {e}")

    def load_models(self) -> Dict[str, bool]:
        """Load all saved models from disk."""
        results = {}

        for model_file in self.models_dir.glob("*_correlation_model.pkl"):
            sport = model_file.stem.replace("_correlation_model", "")

            try:
                with open(model_file, "rb") as f:
                    data = pickle.load(f)

                self._models[sport] = data["model"]
                self._scalers[sport] = data.get("scaler")
                self._encoders[sport] = data.get("encoders", {})
                self._metrics[sport] = data.get("metrics", {})

                results[sport] = True
                logger.info(f"Loaded {sport} model from {model_file}")
            except Exception as e:
                logger.warning(f"Failed to load {model_file}: {e}")
                results[sport] = False

        return results

    def get_model_summary(self) -> Dict:
        """Get summary of trained models."""
        summary = {
            "models_trained": list(self._models.keys()),
            "models_dir": str(self.models_dir),
        }

        for sport, metrics in self._metrics.items():
            summary[sport] = {
                "samples": metrics.get("samples", 0),
                "test_r2": round(metrics.get("test_r2", 0), 3),
                "test_mae": round(metrics.get("test_mae", 0), 3),
                "cv_mean": round(metrics.get("cv_mean", 0), 3) if "cv_mean" in metrics else None,
            }

        return summary
